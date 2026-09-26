<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api.js'

const role = ref(localStorage.getItem('role') || '')
const isWriter = computed(() => role.value === 'writer')

const board = ref({ watch: [], in_flight: [], history: [] })
const events = ref([])
const form = ref({ lamp: '氖灯', nominal_nm: 585.25, measured_nm: 585.30 })
const eventLamp = ref('')          // 流水按灯回翻，空 = 全部
const rejected = ref(null)         // 最近一次拒收（内嵌标红，非弹窗）
const admitted = ref('')           // 最近一次放行提示
const err = ref('')
let timer

function fmt(ts) {
  return ts ? String(ts).replace('T', ' ').slice(0, 19) : ''
}

async function refresh() {
  if (!localStorage.getItem('tok')) return
  try {
    const q = eventLamp.value.trim() ? `?lamp=${encodeURIComponent(eventLamp.value.trim())}` : ''
    const [b, ev] = await Promise.all([
      api('/api/mutex/board'),
      api(`/api/mutex/events${q}`),
    ])
    board.value = b
    events.value = ev
    err.value = ''
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function submit() {
  rejected.value = null
  admitted.value = ''
  err.value = ''
  try {
    const res = await api('/api/jobs', { method: 'POST', body: JSON.stringify(form.value) })
    admitted.value = `已放行：同灯无未结编号，新单 #${res.id} 进入在途`
  } catch (e) {
    if (e.status === 409) {
      // 提交前先问该灯是否仍有未结编号：有则拒收并列出冲突号
      rejected.value = {
        lamp: e.data?.lamp || form.value.lamp,
        detail: String(e.message || e),
        conflictIds: e.data?.conflict_ids || [],
      }
    } else {
      err.value = String(e.message || e)
    }
  }
  await refresh()
}

function filterEventsBy(lamp) {
  eventLamp.value = lamp
  refresh()
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
    <p class="rule">同一灯种同时只允许一个未结编号；提交前先查该灯，仍有未结编号即拒收并列出冲突号，结案后才允许同灯再开。</p>

    <!-- 提交区：仅校准员 -->
    <section v-if="isWriter" class="card submit-card">
      <h3>提交校准</h3>
      <div class="form-row">
        <label>灯种 <input v-model="form.lamp" placeholder="如 氖灯" /></label>
        <label>标称 nm <input type="number" step="0.01" v-model.number="form.nominal_nm" /></label>
        <label>实测 nm <input type="number" step="0.01" v-model.number="form.measured_nm" /></label>
        <button type="button" @click="submit">提交</button>
      </div>
      <!-- 拒收：内嵌标红，非弹窗 -->
      <div v-if="rejected" class="reject-bar">
        <strong>拒收（同灯互斥）</strong>
        <span>灯种「{{ rejected.lamp }}」仍有未结编号，须结案后再开。</span>
        <span class="conflict">冲突编号：
          <em v-for="cid in rejected.conflictIds" :key="cid" class="conflict-id">#{{ cid }}</em>
        </span>
      </div>
      <div v-if="admitted" class="admit-bar">{{ admitted }}</div>
    </section>
    <section v-else class="card readonly-note">巡检员只读：可查看三块与流水，不可提交。</section>

    <p v-if="err" class="err">{{ err }}</p>

    <!-- 三块 -->
    <div class="three-cols">
      <section class="panel watch">
        <h3>冲突监视栏</h3>
        <p class="muted">近 10 分钟有未结占用 / 被拒收的灯</p>
        <table>
          <thead><tr><th>灯种</th><th>未结编号</th><th>拒收次数</th></tr></thead>
          <tbody>
            <tr v-for="w in board.watch" :key="w.lamp" :class="{ hot: w.reject_count > 0 }">
              <td><a href="#" @click.prevent="filterEventsBy(w.lamp)">{{ w.lamp }}</a></td>
              <td>
                <span v-if="w.open_ids.length" class="open-ids">
                  <em v-for="cid in w.open_ids" :key="cid">#{{ cid }}</em>
                </span>
                <span v-else class="muted">—</span>
              </td>
              <td :class="{ 'rej-num': w.reject_count > 0 }">{{ w.reject_count }}</td>
            </tr>
            <tr v-if="!board.watch.length"><td colspan="3" class="muted">暂无冲突</td></tr>
          </tbody>
        </table>
      </section>

      <section class="panel inflight">
        <h3>在途同灯栏</h3>
        <p class="muted">已放行、尚未结案的编号</p>
        <table>
          <thead><tr><th>编号</th><th>灯种</th><th>标称</th><th>实测</th><th>提交人</th><th>时间</th></tr></thead>
          <tbody>
            <tr v-for="j in board.in_flight" :key="j.id">
              <td>#{{ j.id }}</td>
              <td>{{ j.lamp }}</td>
              <td>{{ j.nominal_nm }}</td>
              <td>{{ j.measured_nm }}</td>
              <td>{{ j.created_by }}</td>
              <td>{{ fmt(j.created_at) }}</td>
            </tr>
            <tr v-if="!board.in_flight.length"><td colspan="6" class="muted">在途为空</td></tr>
          </tbody>
        </table>
      </section>

      <section class="panel history">
        <h3>历史已结案栏</h3>
        <p class="muted">结案后旧单进入此处</p>
        <table>
          <thead><tr><th>编号</th><th>灯种</th><th>结论</th><th>理由</th><th>时间</th></tr></thead>
          <tbody>
            <tr v-for="j in board.history" :key="j.id">
              <td>#{{ j.id }}</td>
              <td>{{ j.lamp }}</td>
              <td :class="j.verdict === '超差' ? 'bad' : 'good'">{{ j.verdict }}</td>
              <td>{{ j.reason }}</td>
              <td>{{ fmt(j.created_at) }}</td>
            </tr>
            <tr v-if="!board.history.length"><td colspan="5" class="muted">暂无结案</td></tr>
          </tbody>
        </table>
      </section>
    </div>

    <!-- 流水：冲突与放行各记流水，可按灯回翻 -->
    <section class="panel events">
      <h3>互斥流水（冲突 / 放行）</h3>
      <div class="filter-row">
        <label>按灯回翻
          <input v-model="eventLamp" placeholder="灯种，留空看全部" @keyup.enter="refresh" />
        </label>
        <button type="button" @click="refresh">查询</button>
        <button type="button" @click="filterEventsBy('')">全部</button>
      </div>
      <table>
        <thead>
          <tr><th>流水号</th><th>时间</th><th>灯种</th><th>类型</th><th>编号</th><th>冲突号</th><th>操作人</th><th>详情</th></tr>
        </thead>
        <tbody>
          <tr v-for="e in events" :key="e.id" :class="{ 'row-reject': e.kind === 'reject', 'row-admit': e.kind === 'admit' }">
            <td>{{ e.id }}</td>
            <td>{{ fmt(e.created_at) }}</td>
            <td>{{ e.lamp }}</td>
            <td class="kind">{{ e.kind === 'admit' ? '放行' : '冲突拒收' }}</td>
            <td>{{ e.job_id ? '#' + e.job_id : '—' }}</td>
            <td>
              <template v-if="e.conflict_ids && e.conflict_ids.length">
                <em v-for="cid in e.conflict_ids" :key="cid" class="conflict-id">#{{ cid }}</em>
              </template>
              <span v-else>—</span>
            </td>
            <td>{{ e.actor }}</td>
            <td>{{ e.detail }}</td>
          </tr>
          <tr v-if="!events.length"><td colspan="8" class="muted">暂无流水</td></tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.mutex-page h2 { margin: 4px 0 2px; }
.rule { color: #555; font-size: 13px; margin: 0 0 14px; }
.card { border: 1px solid #ccc; border-radius: 6px; padding: 12px 14px; margin-bottom: 14px; background: #fff; }
.submit-card h3, .panel h3 { margin: 0 0 8px; }
.form-row { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
.form-row label { display: flex; flex-direction: column; font-size: 13px; gap: 4px; }
.form-row input { padding: 4px 6px; }
.readonly-note { background: #f4f6f8; color: #555; }
.reject-bar {
  margin-top: 10px; padding: 10px 12px; border: 1px solid #d93025;
  background: #fdecea; color: #b00020; border-radius: 4px;
  display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
}
.conflict .conflict-id, .open-ids em, .conflict-id {
  font-style: normal; font-weight: 700; margin: 0 4px;
}
.conflict-id { color: #b00020; }
.admit-bar { margin-top: 10px; padding: 8px 12px; background: #e6f4ea; color: #1e6b3a; border: 1px solid #95c7a6; border-radius: 4px; }
.err { color: #b00020; }
.muted { color: #888; }

.three-cols { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }
@media (max-width: 1100px) { .three-cols { grid-template-columns: 1fr; } }
.panel { border: 1px solid #ccc; border-radius: 6px; padding: 12px 14px; background: #fff; }
.three-cols .panel { margin-bottom: 0; }
.events { margin-top: 14px; }
.panel table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.panel th, .panel td { border: 1px solid #e2e2e2; padding: 5px 7px; text-align: left; vertical-align: top; }
.panel th { background: #f4f6f8; }
.watch tr.hot { background: #fff7f6; }
.rej-num { color: #b00020; font-weight: 700; }
.bad { color: #b00020; font-weight: 700; }
.good { color: #1e6b3a; font-weight: 700; }
.row-reject { background: #fdecea; }
.row-reject .kind { color: #b00020; font-weight: 700; }
.row-admit .kind { color: #1e6b3a; font-weight: 700; }
.filter-row { display: flex; gap: 10px; align-items: center; margin-bottom: 10px; }
.filter-row label { font-size: 13px; }
.filter-row input { padding: 4px 6px; }
</style>
