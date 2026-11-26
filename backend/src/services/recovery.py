"""已签批次的离线备份与演练库恢复。

设计要点（对应业务口径）：

* 只允许备份复核员已签署的证据批次；备份包与档案岗另存的校验凭据分离，
  凭据是对备份包规范化字节的 SHA-256，不随包下发。
* 凭据覆盖每张凭条的入园日期与每个分区的完整边界，张数相同但日期或边界
  被改动，重算结果必然不一致，恢复在切换前即被拒绝。
* 恢复在单条数据库事务内完成：重建持久化分区父表、待核池与摘除表，
  空分区照样建表；逐张回读并与清单对账，全部通过后才一次性切换指针。
* 任一步失败整笔回滚，演练库里上一批可查数据不受影响。
"""
import hashlib
import re
from datetime import date
from uuid import uuid4

from sqlalchemy import text

from src.utils.encoding import canonical
from src.utils.time import utc_time

BUNDLE_VERSION = 1
SCHEMA_PREFIX = 'restore_'
# 恢复批次在恢复目录中的状态。
ACTIVE = 'active'
SUPERSEDED = 'superseded'
# 恢复后分区类型。
PART_ONLINE = 'online'
PART_ARCHIVE = 'archive'
PART_POOL = 'pool'
_HEX64 = re.compile(r'^[0-9a-f]{64}$')
_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')


class RecoveryError(ValueError):
    """备份包或校验凭据不符合恢复条件。"""


def root_digest(bundle):
    """档案岗另存的校验凭据：对整个备份包规范化字节求 SHA-256。

    规范化字节中包含每张凭条的入园日期和每个分区的完整边界，
    因此任何日期或分区边界改动都会使凭据失配，与张数无关。
    """
    return hashlib.sha256(canonical(bundle)).hexdigest()


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------

def _month_section(report):
    for section in report['sections']:
        if section['title'] == '月分区处置':
            return section['rows']
    raise RecoveryError('证据缺少「月分区处置」段落，无法备份')


def _slip_section(report):
    for section in report['sections']:
        if section['title'] == '逐张凭条去向':
            return section['rows']
    raise RecoveryError('证据缺少「逐张凭条去向」段落，无法备份')


def _pool_label(report):
    # domain.py 中 DEFAULT 待核池的标签在逐张路由列里复用。
    for section in report['sections']:
        for row in section['rows']:
            for key in ('初始路由', '挂载后路由', '最终存放'):
                value = row.get(key)
                if isinstance(value, str) and value.endswith('待核池'):
                    return value
    return 'DEFAULT 待核池'


def build_bundle(evidence_row):
    """从已签证据行构造离线备份包（不含凭据本身）。"""
    report = evidence_row['report']
    if not isinstance(report, dict) or 'metrics' not in report or 'sections' not in report:
        raise RecoveryError('证据报告结构不完整，无法备份')
    pool_label = _pool_label(report)
    # 月份 -> 分区处置类型，作为逐张凭条在线/摘除归属的权威来源，
    # 不依赖展示文案，避免仅凭「核验结果」文本误判。
    month_kind = {}
    for row in _month_section(report):
        if row.get('处置') == '整月到期，已摘除':
            month_kind[row['分区月份']] = PART_ARCHIVE
        else:
            month_kind[row['分区月份']] = PART_ONLINE

    partitions = []
    for row in _month_section(report):
        try:
            lower = row['下界（含）']
            upper = row['上界（不含）']
            # 日期在入包前先做一次严格校验，防止备份包本身不可恢复。
            date.fromisoformat(lower)
            date.fromisoformat(upper)
        except (KeyError, TypeError, ValueError):
            raise RecoveryError('月分区边界不完整或不是有效日期') from None
        is_archive = row.get('处置') == '整月到期，已摘除'
        mounted_after = row.get('挂载后数据库边界')
        # 略过挂载的到期月份在任何时点都没有物理分区，不进入恢复结构。
        never_mounted = mounted_after in (None, '未挂载')
        if never_mounted and not is_archive:
            continue
        partitions.append({
            'month': row['分区月份'],
            'lower': lower,
            'upper': upper,
            'state': row['登记状态'],
            'kind': PART_ARCHIVE if is_archive else PART_ONLINE,
            'decision': row.get('处置', ''),
        })

    slips = []
    seen_ids = set()
    for row in _slip_section(report):
        slip_id = row.get('凭条编号')
        entered_on = row.get('入园日期')
        destination = row.get('最终存放')
        if not isinstance(slip_id, str) or not slip_id or slip_id in seen_ids:
            raise RecoveryError('凭条编号缺失或重复，无法备份')
        try:
            date.fromisoformat(entered_on)
        except (TypeError, ValueError):
            raise RecoveryError(f'凭条 {slip_id} 入园日期无效') from None
        seen_ids.add(slip_id)
        if destination == pool_label:
            kind, partition_month = PART_POOL, None
        else:
            partition_month = entered_on[:7]
            kind = month_kind.get(partition_month)
            if kind is None:
                raise RecoveryError(
                    f'凭条 {slip_id} 所在月份 {partition_month} 缺少分区处置，无法备份')
        slips.append({
            'slip_id': slip_id,
            'entered_on': entered_on,
            'kind': kind,
            'partition_month': partition_month,
            'initial_route': row.get('初始路由'),
            'final_route': destination,
            'verification': row.get('核验结果'),
        })

    metrics = report['metrics']
    # 备份包对同一份已签证据必须逐字节确定：batch_id 与建档时间都从证据行派生，
    # 这样 /backup 与 /credential 多次下载得到的凭据始终一致，可离线验真。
    batch_id = f'batch-{evidence_row["run_id"]}-{evidence_row["id"]:06d}'
    bundle = {
        'format': 'signed-slip-batch',
        'version': BUNDLE_VERSION,
        'batch_id': batch_id,
        'created_at': evidence_row.get('reviewed_at') or utc_time(),
        'source': {
            'evidence_id': evidence_row['id'],
            'run_id': evidence_row['run_id'],
            'evidence_digest': evidence_row['digest'],
            'summary': report.get('summary', ''),
        },
        'cutoff': _extract_cutoff(report),
        'metrics': {
            'slip_count': metrics.get('收到凭条'),
            # domain 口径：在线凭条包含在线分区与仍挂在父表上的待核池；
            # 摘除凭条仅统计已脱离父表的摘除表。
            'online_count': metrics.get('在线凭条'),
            'archive_count': metrics.get('摘除凭条'),
            'pool_count': metrics.get('剩余待核凭条'),
        },
        'partitions': partitions,
        'slips': slips,
    }
    validate_bundle(bundle)
    return bundle


def _extract_cutoff(report):
    summary = report.get('summary', '')
    match = re.search(r'(\d{4}-\d{2}-\d{2})', summary)
    if not match:
        raise RecoveryError('证据摘要中缺少保留截止日，无法备份')
    return match.group(1)


# ---------------------------------------------------------------------------
# 备份包校验（恢复前，逐字段核对，拒绝任何篡改）
# ---------------------------------------------------------------------------

def _require(condition, message):
    if not condition:
        raise RecoveryError(message)


def validate_bundle(bundle):
    _require(isinstance(bundle, dict), '备份包须为 JSON 对象')
    _require(bundle.get('format') == 'signed-slip-batch', '备份包格式标识不符')
    _require(bundle.get('version') == BUNDLE_VERSION, '备份包版本不受支持')
    batch_id = bundle.get('batch_id')
    _require(isinstance(batch_id, str) and bool(re.fullmatch(r'[A-Za-z0-9._-]{1,40}', batch_id)),
             '备份包批次编号无效')
    cutoff = bundle.get('cutoff')
    try:
        cutoff_day = date.fromisoformat(cutoff)
        _require(cutoff_day.isoformat() == cutoff, '保留截止日须为 YYYY-MM-DD')
    except (TypeError, ValueError):
        raise RecoveryError('保留截止日无效') from None

    partitions = bundle.get('partitions')
    _require(isinstance(partitions, list), '分区清单须为数组')
    seen_ranges = {}
    for index, part in enumerate(partitions, 1):
        _require(isinstance(part, dict), f'第 {index} 个分区不是对象')
        lower, upper = part.get('lower'), part.get('upper')
        try:
            lower_day, upper_day = date.fromisoformat(lower), date.fromisoformat(upper)
        except (TypeError, ValueError):
            raise RecoveryError(f'分区 {part.get("month", index)} 边界不是有效日期') from None
        _require(lower_day.day == 1, f'分区 {part.get("month", index)} 下界须为月初')
        _require(upper_day > lower_day, f'分区 {part.get("month", index)} 上界须晚于下界')
        _require(part.get('kind') in (PART_ONLINE, PART_ARCHIVE),
                 f'分区 {part.get("month", index)} 类型无效')
        if lower_day in seen_ranges:
            raise RecoveryError(f'分区边界 {lower} 重复')
        seen_ranges[lower_day] = (upper_day, part['kind'], part.get('month'))

    slips = bundle.get('slips')
    _require(isinstance(slips, list), '凭条清单须为数组')
    seen_slips = set()
    counted = {PART_ONLINE: 0, PART_ARCHIVE: 0, PART_POOL: 0}
    for index, slip in enumerate(slips, 1):
        slip_id = slip.get('slip_id') if isinstance(slip, dict) else None
        _require(isinstance(slip_id, str) and slip_id and slip_id == slip_id.strip(),
                 f'第 {index} 张凭条编号无效')
        _require(slip_id not in seen_slips, f'凭条编号 {slip_id} 重复')
        seen_slips.add(slip_id)
        try:
            entered = date.fromisoformat(slip.get('entered_on'))
            _require(entered.isoformat() == slip.get('entered_on'),
                     f'凭条 {slip_id} 入园日期须为 YYYY-MM-DD')
        except (TypeError, ValueError):
            raise RecoveryError(f'凭条 {slip_id} 入园日期无效') from None
        kind = slip.get('kind')
        _require(kind in counted, f'凭条 {slip_id} 存放类型无效')
        month_start = entered.replace(day=1)
        if kind == PART_POOL:
            _require(slip.get('partition_month') is None,
                     f'待核凭条 {slip_id} 不应携带分区月份')
            _require(month_start not in seen_ranges,
                     f'待核凭条 {slip_id} 却落在已建分区 {month_start:%Y-%m}，边界已被改动')
        else:
            _require(slip.get('partition_month') == month_start.isoformat()[:7],
                     f'凭条 {slip_id} 的分区归属与入园日期不一致')
            range_info = seen_ranges.get(month_start)
            _require(range_info is not None, f'凭条 {slip_id} 所在月份缺少分区，分区边界已被改动')
            _require(range_info[1] == kind,
                     f'凭条 {slip_id} 的在线/摘除归属与分区处置不一致')
        counted[kind] += 1

    metrics = bundle.get('metrics')
    _require(isinstance(metrics, dict), '备份包统计须为对象')
    expected_total = len(slips)
    _require(metrics.get('slip_count') == expected_total, '备份包统计张数与凭条清单不一致')
    # 在线张数含待核池（待核池仍挂在父表上），与 domain 的「在线凭条」口径一致。
    _require(metrics.get('online_count') == counted[PART_ONLINE] + counted[PART_POOL],
             '在线张数统计与逐张归属不一致')
    _require(metrics.get('archive_count') == counted[PART_ARCHIVE], '摘除张数统计与逐张归属不一致')
    _require(metrics.get('pool_count') == counted[PART_POOL], '待核张数统计与逐张归属不一致')
    return counted


def validate_credential(credential):
    if not isinstance(credential, str) or not _HEX64.fullmatch(credential.strip()):
        raise RecoveryError('校验凭据须为 64 位十六进制 SHA-256')
    return credential.strip().lower()


# ---------------------------------------------------------------------------
# 离线恢复（单事务，原子切换）
# ---------------------------------------------------------------------------

def _qualified(identifier):
    if not _IDENT.fullmatch(identifier):
        raise RecoveryError(f'恢复对象标识非法：{identifier!r}')
    return identifier


def _list_orphan_schemas(conn):
    """找出历次中断遗留、且未登记为任何批次的 staging schema。

    LIKE 模式中用反斜杠转义下划线，避免下划线被当作单字符通配符；
    Python 字面量 ``'\\_'`` 即「一个反斜杠 + 下划线」，
    转义字符通过绑定参数传入，规避 SQL 字面量反斜杠的歧义。
    """
    # 转义前缀末尾的下划线（SCHEMA_PREFIX 仅含这一个下划线），
    # 再拼 LIKE 的 '%'；转义字符通过绑定参数传入，规避 SQL 字面量反斜杠歧义。
    pattern = SCHEMA_PREFIX.replace('_', '\_') + '%'
    rows = conn.execute(text("""
        SELECT nspname FROM pg_catalog.pg_namespace
        WHERE nspname LIKE :prefix ESCAPE :escape
    """), {'prefix': pattern, 'escape': '\\'}).tuples().all()
    registered = {row[0] for row in conn.execute(text(
        'SELECT schema_name FROM restored_batches')).tuples().all()}
    return [name for (name,) in rows if name not in registered]


def restore_bundle(engine, bundle, credential, actor, batches_table, audit_log):
    """在演练库离线恢复一个备份包，成功后原子切换为当前可查批次。"""
    counted = validate_bundle(bundle)
    expected_digest = validate_credential(credential)
    actual_digest = root_digest(bundle)
    if actual_digest != expected_digest:
        raise RecoveryError('校验凭据不匹配：凭条日期或分区边界可能已被改动，拒绝恢复')

    source = bundle.get('source') if isinstance(bundle.get('source'), dict) else {}
    schema_name = f'{SCHEMA_PREFIX}{uuid4().hex}'
    parent = 'slip_parent'
    pool_table = 'slip_default'
    table_names = {}
    for part in bundle['partitions']:
        table_names[part['lower']] = 'm_' + part['lower'].replace('-', '')

    with engine.begin() as conn:
        # 同一备份包（batch_id）不允许重复恢复；重复导入在动结构前直接拒绝。
        duplicate = conn.execute(
            batches_table.select().where(batches_table.c.batch_id == bundle['batch_id'])
            .limit(1)).first()
        if duplicate is not None:
            raise RecoveryError('该备份批次已恢复，不能重复切换；如需重查请使用现有批次')

        # 清理上一次中断遗留的未登记 schema；已登记批次的 schema 一律保留。
        for orphan in _list_orphan_schemas(conn):
            conn.execute(text(f'DROP SCHEMA {_qualified(orphan)} CASCADE'))

        conn.execute(text("SET LOCAL DateStyle = 'ISO, YMD'"))
        conn.execute(text(f'CREATE SCHEMA {_qualified(schema_name)}'))
        qualified = f'{_qualified(schema_name)}.{_qualified(parent)}'
        conn.execute(text(f'''
            CREATE TABLE {qualified} (
                slip_id text NOT NULL, entered_on date NOT NULL
            ) PARTITION BY RANGE (entered_on)
        '''))
        # 空分区也保留：无论有无凭条，在册的在线/摘除分区都按边界建表。
        for part in bundle['partitions']:
            table = table_names[part['lower']]
            conn.execute(text(
                f'CREATE TABLE {schema_name}.{_qualified(table)} '
                f"PARTITION OF {qualified} FOR VALUES FROM (DATE '{part['lower']}') "
                f"TO (DATE '{part['upper']}')"))
        conn.execute(text(
            f'CREATE TABLE {schema_name}.{_qualified(pool_table)} '
            f'PARTITION OF {qualified} DEFAULT'))

        if bundle['slips']:
            conn.execute(text(
                f'INSERT INTO {qualified} (slip_id, entered_on) '
                'VALUES (:slip_id, :entered_on)'),
                [{'slip_id': slip['slip_id'], 'entered_on': slip['entered_on']}
                 for slip in bundle['slips']])

        # 摘除表脱离父表独立留存；在线分区（含空分区）保持挂载。
        for part in bundle['partitions']:
            if part['kind'] == PART_ARCHIVE:
                table = table_names[part['lower']]
                conn.execute(text(
                    f'ALTER TABLE {qualified} DETACH PARTITION {schema_name}.{_qualified(table)}'))

        _reconcile(conn, schema_name, parent, pool_table, table_names, bundle, counted)

        now = utc_time()
        # 一次性切换：先把旧可查批次标记为历史，再登记当前批次，同一事务提交。
        conn.execute(batches_table.update().where(batches_table.c.status == ACTIVE)
                     .values(status=SUPERSEDED))
        batch_id = bundle['batch_id']
        restored_id = conn.execute(batches_table.insert().values(
            batch_id=batch_id, schema_name=schema_name,
            evidence_id=source.get('evidence_id'), run_id=source.get('run_id'),
            root_digest=actual_digest, evidence_digest=source.get('evidence_digest', ''),
            slip_count=len(bundle['slips']),
            # 在线张数含待核池；待核张数单列，便于页面分别呈现。
            online_count=counted[PART_ONLINE] + counted[PART_POOL],
            archive_count=counted[PART_ARCHIVE],
            pool_count=counted[PART_POOL], status=ACTIVE, operator=actor,
            created_at=now)).inserted_primary_key[0]
        audit_log(conn, actor, 'restore_batch', restored_id)
        return restored_id, batch_id, schema_name


def _reconcile(conn, schema_name, parent, pool_table, table_names, bundle, counted):
    """逐张回读，核对凭条日期、物理归属和分区边界，任一不符即抛错回滚。"""
    qualified_parent = f'{schema_name}.{parent}'
    # 在线父表（含待核池）回读。
    online = {row[0]: (row[1], row[2]) for row in conn.execute(text(f'''
        SELECT s.slip_id, s.entered_on, c.relname
        FROM {qualified_parent} AS s
        JOIN pg_catalog.pg_class AS c ON c.oid = s.tableoid
    ''')).tuples().all()}
    # 摘除表逐张回读。
    archived = {}
    for part in bundle['partitions']:
        if part['kind'] != PART_ARCHIVE:
            continue
        table = table_names[part['lower']]
        for slip_id, entered_on in conn.execute(text(
                f'SELECT slip_id, entered_on FROM {schema_name}.{_qualified(table)}'
                )).tuples().all():
            if slip_id in archived:
                raise RecoveryError(f'恢复对账失败，凭条 {slip_id} 在摘除表中重复')
            archived[slip_id] = (entered_on, table)

    if set(online) & set(archived):
        raise RecoveryError('恢复对账失败，凭条同时存在于在线账簿和摘除表')
    restored = online | archived
    expected_ids = {slip['slip_id'] for slip in bundle['slips']}
    if set(restored) != expected_ids:
        raise RecoveryError('恢复对账失败，凭条编号集合与备份包不一致')

    for slip in bundle['slips']:
        slip_id = slip['slip_id']
        entered_on, leaf = restored[slip_id]
        if entered_on.isoformat() != slip['entered_on']:
            raise RecoveryError(f'恢复对账失败，凭条 {slip_id} 入园日期与备份包不一致')
        if slip['kind'] == PART_POOL:
            expected_leaf = pool_table
        else:
            month_start = date.fromisoformat(slip['entered_on']).replace(day=1).isoformat()
            expected_leaf = table_names[month_start]
        if leaf != expected_leaf:
            raise RecoveryError(f'恢复对账失败，凭条 {slip_id} 的物理归属与分区边界不一致')

    if (len(online) != counted[PART_ONLINE] + counted[PART_POOL]
            or len(archived) != counted[PART_ARCHIVE]):
        raise RecoveryError('恢复对账失败，在线/摘除/待核张数与备份包统计不一致')

    # 核对 PostgreSQL 实际分区目录：在线分区（含空分区）与待核池仍挂载，摘除表已脱离。
    bounds = dict(conn.execute(text('''
        SELECT child.relname, pg_catalog.pg_get_expr(child.relpartbound, child.oid)
        FROM pg_catalog.pg_inherits AS edge
        JOIN pg_catalog.pg_class AS child ON child.oid = edge.inhrelid
        WHERE edge.inhparent = pg_catalog.to_regclass(:parent)
    '''), {'parent': qualified_parent}).tuples().all())
    expected_mounted = {pool_table, *(table_names[p['lower']]
                                      for p in bundle['partitions']
                                      if p['kind'] == PART_ONLINE)}
    if set(bounds) != expected_mounted:
        raise RecoveryError('恢复对账失败，分区目录与备份包边界不一致')
    for part in bundle['partitions']:
        table = table_names[part['lower']]
        if part['kind'] == PART_ARCHIVE and table in bounds:
            raise RecoveryError(f'恢复对账失败，摘除表 {table} 仍挂在父表上')
        if part['kind'] == PART_ONLINE:
            bound = bounds.get(table, '')
            if part['upper'] not in bound or part['lower'] not in bound:
                raise RecoveryError(f'恢复对账失败，分区 {part["month"]} 边界与备份包不一致')


# ---------------------------------------------------------------------------
# 恢复后的只读查询
# ---------------------------------------------------------------------------

def active_batch(conn, batches_table):
    row = conn.execute(
        batches_table.select().where(batches_table.c.status == ACTIVE)
        .order_by(batches_table.c.id.desc()).limit(1)).mappings().first()
    return dict(row) if row else None


def read_partitions(conn, batch):
    schema_name = _qualified(batch['schema_name'])
    rows = conn.execute(text('''
        SELECT child.relname,
               pg_catalog.pg_get_expr(child.relpartbound, child.oid) AS bound
        FROM pg_catalog.pg_inherits AS edge
        JOIN pg_catalog.pg_class AS child ON child.oid = edge.inhrelid
        WHERE edge.inhparent = pg_catalog.to_regclass(:parent)
        ORDER BY child.relname
    '''), {'parent': f'{schema_name}.slip_parent'}).tuples().all()
    return [{'table': name, 'bound': bound} for name, bound in rows]


def read_slips(conn, batch, kind):
    schema_name = _qualified(batch['schema_name'])
    if kind == 'online':
        sql = f'''
            SELECT s.slip_id, s.entered_on, c.relname AS leaf, false AS archived
            FROM {schema_name}.slip_parent AS s
            JOIN pg_catalog.pg_class AS c ON c.oid = s.tableoid
            ORDER BY s.entered_on, s.slip_id
        '''
    elif kind == 'archive':
        # 摘除表已脱离父表，逐表 UNION 回读。
        tables = [row[0] for row in conn.execute(text('''
            SELECT c.relname FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = :schema AND c.relname LIKE 'm_%'
            AND NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_inherits AS e
                WHERE e.inhrelid = c.oid)
            ORDER BY c.relname
        '''), {'schema': batch['schema_name']}).tuples().all()]
        if not tables:
            return []
        pieces = ' UNION ALL '.join(
            f"SELECT slip_id, entered_on, '{name}' AS leaf, true AS archived "
            f'FROM {schema_name}.{_qualified(name)}' for name in tables)
        sql = f'SELECT * FROM ({pieces}) AS archived ORDER BY entered_on, slip_id'
    else:
        raise RecoveryError('读取类型须为 online 或 archive')
    return [dict(row._mapping) for row in conn.execute(text(sql)).all()]
