import {heading, panel} from '../components/common/heading.js';
import {entries, records} from '../components/common/records.js';
import {topic} from '../constants/project.js';
import {date, el, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

const restoreStatusText = {active: '当前可查', superseded: '历史批次'};

async function downloadJson(api, path, filename, fail) {
  try {
    const response = await api(path, {response: true});
    const url = URL.createObjectURL(await response.blob());
    const link = el('a');
    link.href = url;
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { fail(error); }
}

async function readJsonFile(input) {
  const file = input.files && input.files[0];
  if (!file) throw new Error('请先选择文件');
  return JSON.parse(await file.text());
}

function activeBatchPanel(state) {
  const batch = state.restore.active;
  if (!batch) {
    return panel('演练库当前可查批次',
      '<p class="empty" data-testid="no-active-batch">演练库尚未恢复任何已签批次，当前无可查批次。</p>');
  }
  const partitionRows = batch.partitions.map(p => [esc(p.table), `<code>${esc(p.bound)}</code>`]);
  const onlineRows = state.restore.online.map(s => [
    esc(s.slip_id), esc(s.entered_on), esc(s.leaf),
    s.leaf === 'slip_default' ? '待核池' : '在线分区']);
  const archiveRows = state.restore.archive.map(s => [
    esc(s.slip_id), esc(s.entered_on), esc(s.leaf), '摘除表']);
  const metrics = [
    ['凭条总数', batch.slip_count], ['在线凭条', batch.online_count],
    ['摘除凭条', batch.archive_count], ['待核凭条', batch.pool_count],
  ].map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('');
  return panel('演练库当前可查批次（回收核验）', `
    <p data-testid="active-batch">当前可查批次：<strong>${esc(batch.batch_id)}</strong>
      （${esc(date(batch.created_at))} 由 ${esc(batch.operator)} 恢复，来源证据 ${esc(batch.evidence_id ?? '—')}）</p>
    <dl class="metrics">${metrics}</dl>
    <h3>分区目录（空分区与待核池均保留）</h3>
    ${records(['物理表', 'PostgreSQL 分区边界'], partitionRows)}
    <h3>在线账簿与待核池（${state.restore.online.length} 张，各归原处）</h3>
    ${records(['凭条编号', '入园日期', '物理表', '归属'], onlineRows, '在线账簿为空。')}
    <h3>摘除表（${state.restore.archive.length} 张）</h3>
    ${records(['凭条编号', '入园日期', '摘除表', '归属'], archiveRows, '没有摘除表。')}`);
}

function restorePanel(state) {
  if (state.session.role !== 'operator') return '';
  return panel('离线恢复到演练库', `
    <p class="note">分别选择离线备份包与档案岗另存的校验凭据。张数相同但凭条日期或分区边界被改动时，
      凭据校验失败会拒绝恢复。恢复在单条事务内一次性切换，恢复中断时上一批照常可查。</p>
    <form id="restore-form">
      <div class="field-grid">
        <label class="field"><span>离线备份包（signed-batch-*.json）</span>
          <input type="file" name="bundle" accept=".json,application/json" required></label>
        <label class="field"><span>校验凭据（credential-*.json）</span>
          <input type="file" name="credential" accept=".json,application/json" required></label>
      </div>
      <div class="actions form-actions"><button>校验并恢复切换</button></div>
    </form>`);
}

function restoreHistoryPanel(state) {
  const rows = state.restore.batches.map(b => [
    esc(b.batch_id),
    `<span class="status" data-state="${esc(b.status)}">${esc(restoreStatusText[b.status] || b.status)}</span>`,
    esc(b.slip_count), esc(b.online_count), esc(b.archive_count), esc(b.pool_count),
    esc(b.operator), esc(date(b.created_at))]);
  return `<details><summary>恢复批次历史（${state.restore.batches.length}）</summary>
    ${records(['批次', '状态', '总张数', '在线', '摘除', '待核', '经办', '恢复时间'], rows, '暂无恢复记录。')}</details>`;
}

export function evidencePage(state, identity) {
  const current = state.evidence.find(row=>row.id===identity) || state.evidence[0];
  if (!current) return heading(topic.modules[3]) + '<p class="empty">执行完成后，可查看、复核和导出证据。</p>';
  const {report} = current;
  const metrics = Object.entries(report.metrics).map(([label,value])=>`<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('');
  const sections = report.sections.map(x=>panel(x.title, entries(x.rows))).join('');
  const review = current.reviewer ? `<p data-testid="reviewed">${esc(current.reviewer)} · ${esc(date(current.reviewed_at))}</p><p>${esc(current.review_note)}</p>`
    : state.session.role==='reviewer'?`<form id="review-form" data-id="${current.id}"><label>复核意见<textarea name="note" maxlength="500" required></textarea></label><button>签署复核</button></form>`
    : '<p class="note">待复核员签署</p>';
  const history = state.evidence.map(row=>[`<a href="#evidence/${row.id}">执行 ${row.run_id}</a>`, esc(row.report.summary), esc(row.reviewer || '待复核')]);
  const backupTools = current.reviewer
    ? `<button id="backup" class="secondary" data-id="${current.id}">下载离线备份包</button>
       <button id="credential" class="secondary" data-id="${current.id}">另存校验凭据</button>` : '';
  return `${heading(topic.modules[3], report.summary,
      `<button id="download" data-id="${current.id}">导出证据</button>${backupTools}`)}
    <div class="spread evidence-top"><dl class="metrics">${metrics}</dl><aside class="brief"><h2>复核签署</h2>${review}<details><summary>结果校验码</summary><code class="digest">${esc(current.digest)}</code></details></aside></div>
    ${activeBatchPanel(state)}
    ${restorePanel(state)}
    ${sections}<details><summary>历史证据</summary>${records(['执行','结论','复核'], history)}</details>
    ${restoreHistoryPanel(state)}`;
}

export function bindEvidence(api, update, fail) {
  const download = document.getElementById('download');
  if (download) {
    download.addEventListener('click', () =>
      downloadJson(api, `/evidence/${download.dataset.id}/download`,
        `evidence-${download.dataset.id}.json`, fail));
  }
  const backup = document.getElementById('backup');
  if (backup) {
    backup.addEventListener('click', () =>
      downloadJson(api, `/evidence/${backup.dataset.id}/backup`,
        `signed-batch-${backup.dataset.id}.json`, fail));
  }
  const credential = document.getElementById('credential');
  if (credential) {
    credential.addEventListener('click', () =>
      downloadJson(api, `/evidence/${credential.dataset.id}/credential`,
        `credential-${credential.dataset.id}.json`, fail));
  }
  submit('review-form', async data=>{
    const form = document.getElementById('review-form');
    await api(`/evidence/${form.dataset.id}/review`, {method:'POST', data:{note:data.get('note')}});
    await update();
  }, fail);
  submit('restore-form', async ()=>{
    const bundleInput = document.querySelector('#restore-form input[name="bundle"]');
    const credentialInput = document.querySelector('#restore-form input[name="credential"]');
    const [bundle, credentialFile] = await Promise.all([
      readJsonFile(bundleInput), readJsonFile(credentialInput)]);
    const rootDigest = credentialFile && credentialFile.root_digest;
    if (!rootDigest) throw new Error('校验凭据文件中缺少 root_digest');
    await api('/restore', {method: 'POST', data: {bundle, credential: rootDigest}});
    await update();
  }, fail);
}
