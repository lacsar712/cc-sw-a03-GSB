<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api.js'

const role = ref(localStorage.getItem('role') || '')
const isWriter = computed(() => role.value === 'writer')

const board = ref({ conflicts: [], inflight: [], history: [] })
const logs = ref([])
const err = ref('')
const rejectMsg = ref('')
const form = ref({ lamp: '', nominal_nm: 0.15, measured_nm: 0.15 })
const lampFilter = ref('')
let timer

const conflictLamps = computed(() => new Set(board.value.conflicts.map((c) => c.lamp)))

async function refresh() {
  if (!localStorage.getItem('tok')) return
  try {
    const q = lampFilter.value.trim() ? `?lamp=${encodeURIComponent(lampFilter.value.trim())}` : ''
    const [b, l] = await Promise.all([api('/api/board'), api(`/api/logs${q}`)])
    board.value = b
    logs.value = l
    err.value = ''
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function submit() {
  rejectMsg.value = ''
  err.value = ''
  if (!form.value.lamp.trim()) {
    rejectMsg.value = '灯种不能为空'
    return
  }
  try {
    await api('/api/jobs', { method: 'POST', body: JSON.stringify(form.value) })
    lampFilter.value = form.value.lamp.trim()
    await refresh()
  } catch (e) {
    // 同灯有未结编号：页内标红拒收并展示冲突号
    rejectMsg.value = String(e.message || e)
    await refresh()
  }
}

async function closeJob(job) {
  rejectMsg.value = ''
  err.value = ''
  try {
    await api(`/api/jobs/${job.id}/close`, { method: 'POST' })
    lampFilter.value = job.lamp
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

function fmt(ts) {
  if (!ts) return ''
  return new Date(ts).toLocaleString('zh-CN', { hour12: false })
}

onMounted(() => {
  role.value = localStorage.getItem('role') || ''
  refresh()
  timer = setInterval(refresh, 1000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="mutex-page">
    <h2>同灯互斥台</h2>
    <p v-if="err" class="err">{{ err }}</p>

    <!-- 拒收标红：页内红条展示冲突号（非弹窗） -->
    <div v-if="rejectMsg" class="reject-banner" role="alert">
      <span class="reject-icon">⛔</span>
      <span class="reject-text">提交被拒收：{{ rejectMsg }}</span>
      <button type="button" class="reject-close" @click="rejectMsg = ''">×</button>
    </div>

    <section v-if="isWriter" class="submit-card">
      <h3>提交校准（同灯排队互斥）</h3>
      <div class="submit-row">
        <label>灯种 <input v-model="form.lamp" placeholder="如：氖灯-640" /></label>
        <label>标称 nm <input type="number" step="0.01" v-model.number="form.nominal_nm" /></label>
        <label>实测 nm <input type="number" step="0.01" v-model.number="form.measured_nm" /></label>
        <button type="button" class="primary" @click="submit">投递</button>
      </div>
      <p class="hint">该灯若仍有未结编号将被拒收，结案后才允许同灯再开。</p>
    </section>
    <section v-else class="readonly-banner">巡检员只读：可查看三栏与流水，不可提交、不可结案。</section>

    <div class="columns">
      <section class="col">
        <h3 class="col-title conflict-title">冲突监视栏 <span class="count">{{ board.conflicts.length }}</span></h3>
        <p v-if="!board.conflicts.length" class="empty">暂无冲突</p>
        <ul v-else class="item-list">
          <li v-for="c in board.conflicts" :key="c.id" class="conflict-item">
            <div class="lamp-line">{{ c.lamp }}</div>
            <div class="detail">{{ c.detail }}</div>
            <div class="meta">{{ c.actor }} · {{ fmt(c.created_at) }}</div>
          </li>
        </ul>
      </section>

      <section class="col">
        <h3 class="col-title inflight-title">在途同灯栏 <span class="count">{{ board.inflight.length }}</span></h3>
        <p v-if="!board.inflight.length" class="empty">在途已清空</p>
        <ul v-else class="item-list">
          <li v-for="j in board.inflight" :key="j.id" class="job-item">
            <div class="lamp-line">
              <span class="badge">#{{ j.id }}</span> {{ j.lamp }}
            </div>
            <div class="detail">标称 {{ j.nominal_nm }} / 实测 {{ j.measured_nm }}</div>
            <div class="meta">{{ j.created_by }} · {{ fmt(j.created_at) }}</div>
            <button v-if="isWriter" type="button" class="close-btn" @click="closeJob(j)">结案</button>
          </li>
        </ul>
      </section>

      <section class="col">
        <h3 class="col-title history-title">历史已结案栏 <span class="count">{{ board.history.length }}</span></h3>
        <p v-if="!board.history.length" class="empty">暂无已结案单</p>
        <ul v-else class="item-list">
          <li
            v-for="j in board.history"
            :key="j.id"
            class="job-item"
            :class="{ lampflag: conflictLamps.has(j.lamp) }"
          >
            <div class="lamp-line">
              <span class="badge">#{{ j.id }}</span> {{ j.lamp }}
              <span class="verdict" :class="j.verdict === '合格' ? 'ok' : 'bad'">{{ j.verdict }}</span>
            </div>
            <div class="detail">{{ j.reason }}</div>
            <div class="meta">{{ j.created_by }} · 结案 {{ fmt(j.closed_at) }}</div>
          </li>
        </ul>
      </section>
    </div>

    <section class="logs-card">
      <div class="logs-head">
        <h3>流水（冲突 / 放行，可按灯回翻）</h3>
        <div class="filter">
          <input v-model="lampFilter" placeholder="按灯种过滤，如 氖灯-640" @keyup.enter="refresh" />
          <button type="button" @click="refresh">回翻</button>
        </div>
      </div>
      <table class="logs-table">
        <thead>
          <tr><th>时间</th><th>灯种</th><th>类型</th><th>关联单</th><th>明细</th><th>操作人</th></tr>
        </thead>
        <tbody>
          <tr v-for="l in logs" :key="l.id" :class="l.kind">
            <td>{{ fmt(l.created_at) }}</td>
            <td>{{ l.lamp }}</td>
            <td>
              <span class="kind-tag" :class="l.kind">{{ l.kind === 'conflict' ? '冲突' : '放行' }}</span>
            </td>
            <td>{{ l.job_id ? '#' + l.job_id : '—' }}</td>
            <td>{{ l.detail }}</td>
            <td>{{ l.actor }}</td>
          </tr>
          <tr v-if="!logs.length"><td colspan="6" class="empty">暂无流水</td></tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.mutex-page {
  max-width: 1180px;
  margin: 0 auto;
}
h2 {
  margin: 8px 0 16px;
}
.err {
  color: #b00020;
  font-weight: 600;
}
.submit-card {
  border: 1px solid #c7d2dd;
  border-radius: 8px;
  padding: 14px 16px;
  margin-bottom: 16px;
  background: #f8fafc;
}
.submit-card h3 {
  margin: 0 0 10px;
}
.submit-row {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
}
.submit-row label {
  font-size: 14px;
  color: #334155;
}
.submit-row input {
  margin-left: 6px;
  padding: 4px 8px;
  border: 1px solid #b6c2cf;
  border-radius: 4px;
}
button {
  cursor: pointer;
  padding: 5px 14px;
  border-radius: 4px;
  border: 1px solid #7a8ea3;
  background: #fff;
}
button.primary {
  background: #1d4ed8;
  border-color: #1d4ed8;
  color: #fff;
}
.hint {
  margin: 8px 0 0;
  color: #64748b;
  font-size: 13px;
}
.readonly-banner {
  background: #eef2f7;
  border: 1px dashed #94a3b8;
  border-radius: 8px;
  padding: 12px 16px;
  color: #475569;
  margin-bottom: 16px;
}
.columns {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 14px;
}
.col {
  border: 1px solid #d4dde7;
  border-radius: 8px;
  padding: 12px;
  background: #fff;
  min-height: 220px;
}
.col-title {
  margin: 0 0 10px;
  font-size: 15px;
  padding-bottom: 8px;
  border-bottom: 2px solid #e2e8f0;
}
.conflict-title { border-bottom-color: #dc2626; }
.inflight-title { border-bottom-color: #d97706; }
.history-title { border-bottom-color: #16a34a; }
.count {
  font-size: 12px;
  color: #64748b;
  font-weight: 400;
}
.empty {
  color: #94a3b8;
  font-size: 13px;
}
.item-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.conflict-item,
.job-item {
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 8px 10px;
  background: #fbfdff;
}
.conflict-item {
  border-color: #f3b4b4;
  background: #fef2f2;
}
.lamp-line {
  font-weight: 600;
  color: #1e293b;
}
.detail {
  font-size: 13px;
  color: #475569;
  margin: 2px 0;
}
.meta {
  font-size: 12px;
  color: #94a3b8;
}
.badge {
  display: inline-block;
  background: #e2e8f0;
  border-radius: 4px;
  padding: 0 6px;
  font-size: 12px;
  margin-right: 4px;
}
.verdict {
  font-size: 12px;
  margin-left: 6px;
}
.verdict.ok { color: #15803d; }
.verdict.bad { color: #b91c1c; }
/* 历史区行标红：仅提示该灯发生过冲突，不替代拒收红条 */
.lampflag {
  border-color: #f3b4b4;
  background: #fff7f7;
}
.close-btn {
  margin-top: 6px;
  font-size: 13px;
  padding: 3px 12px;
}
.logs-card {
  margin-top: 18px;
  border: 1px solid #d4dde7;
  border-radius: 8px;
  padding: 12px 16px;
  background: #fff;
}
.logs-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
}
.logs-head h3 {
  margin: 0;
  font-size: 15px;
}
.filter input {
  padding: 4px 8px;
  border: 1px solid #b6c2cf;
  border-radius: 4px;
  width: 220px;
}
.logs-table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 10px;
  font-size: 13px;
}
.logs-table th,
.logs-table td {
  border: 1px solid #e2e8f0;
  padding: 6px 8px;
  text-align: left;
}
.logs-table tr.conflict td {
  background: #fef2f2;
}
.kind-tag {
  border-radius: 4px;
  padding: 1px 8px;
  font-size: 12px;
}
.kind-tag.conflict {
  background: #dc2626;
  color: #fff;
}
.kind-tag.release {
  background: #16a34a;
  color: #fff;
}
.reject-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  background: #fef2f2;
  border: 2px solid #dc2626;
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 16px;
}
.reject-icon {
  font-size: 18px;
}
.reject-text {
  flex: 1;
  color: #b91c1c;
  font-weight: 700;
  font-size: 15px;
  line-height: 1.6;
}
.reject-close {
  border: none;
  background: transparent;
  color: #b91c1c;
  font-size: 18px;
  font-weight: 700;
  padding: 0 6px;
}
</style>
