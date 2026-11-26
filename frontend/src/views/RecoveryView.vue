<template>
  <section class="page" data-module="failure_quarantine">
    <header class="page-head">
      <div>
        <h2>失败隔离区 · 恢复重排工作台</h2>
        <p class="page-desc">
          按样本真实来源站点/标识与失败阶段筛查可恢复样本，翻页选择后提交重排；
          迟到成功回执由调度确认后才决定补采或核验首份产物。
        </p>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">筛选命中</span>
        <strong class="stat-value">{{ pageData.total }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">真实可重排</span>
        <strong class="stat-value">{{ pageData.eligible_total }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">跨页已选</span>
        <strong class="stat-value">{{ selectedIds.size }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待核验回执</span>
        <strong class="stat-value">{{ heldReceiptCount }}</strong>
      </article>
    </div>

    <!-- 筛选条件：等值匹配真实来源与失败阶段，重名站点用站点标识区分 -->
    <form class="filter-bar" @submit.prevent="reload(1)">
      <label class="filter-item">
        <span>来源站点（真实来源，等值）</span>
        <input v-model="filters.来源站点" data-testid="filter-site" placeholder="如：站点B" />
      </label>
      <label class="filter-item">
        <span>站点标识（区分重名站点）</span>
        <input v-model="filters.站点标识" data-testid="filter-site-id" placeholder="如：SITE-B" />
      </label>
      <label class="filter-item">
        <span>失败阶段</span>
        <select v-model="filters.失败阶段" data-testid="filter-stage">
          <option value="">全部阶段</option>
          <option v-for="stage in stages" :key="stage" :value="stage">{{ stage }}</option>
        </select>
      </label>
      <button class="btn primary" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <div class="toolbar">
      <div class="selection-hint" data-testid="selection-hint">
        已选候选：{{ selectedSummary || '（无）' }}
        <button v-if="selectedIds.size" class="link" type="button" @click="selectedIds.clear()">清空选择</button>
      </div>
      <div class="toolbar-actions">
        <button
          class="btn primary"
          data-testid="submit-reschedule"
          type="button"
          :disabled="!selectedIds.size || submitting"
          @click="submit"
        >
          {{ submitting ? '提交中…' : `提交重排（${selectedIds.size}）` }}
        </button>
      </div>
    </div>

    <table class="data-table recovery-table">
      <thead>
        <tr>
          <th class="col-check">选择</th>
          <th>样本编号</th>
          <th>来源站点</th>
          <th>站点标识</th>
          <th>失败阶段</th>
          <th>失败原因</th>
          <th>重试次数</th>
          <th>隔离状态</th>
          <th>候选资格</th>
          <th>迟到回执</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in pageData.items"
          :key="String(row.id)"
          :data-testid="`row-${row.id}`"
          :data-status="String(row.status)"
          :class="{ 'row-selected': selectedIds.has(row.id) }"
        >
          <td class="col-check">
            <input
              type="checkbox"
              :data-testid="`check-${row.id}`"
              :value="row.id"
              :disabled="row.候选状态 !== '可重排' && row.候选状态 !== '待核验'"
              :checked="selectedIds.has(row.id)"
              @change="toggle(row)"
            />
          </td>
          <td>{{ row.样本编号 }}</td>
          <td>{{ row.来源站点 }}</td>
          <td>{{ row.站点标识 }}</td>
          <td>{{ row.失败阶段 }}</td>
          <td>{{ row.失败原因 }}</td>
          <td>{{ row.重试次数 }}</td>
          <td>{{ row.status }}</td>
          <td>
            <span class="badge" :class="badgeClass(row.候选状态)">{{ row.候选状态 }}</span>
            <span v-if="row.不可选原因" class="cell-reason">{{ row.不可选原因 }}</span>
          </td>
          <td>
            <span v-if="row.回执待确认" class="badge badge-held" data-testid="receipt-pending">
              成功回执待确认
            </span>
            <span v-else-if="row.回执状态">{{ row.回执状态 }}</span>
            <span v-else>—</span>
          </td>
        </tr>
        <tr v-if="!pageData.items.length">
          <td colspan="10" class="empty-state">当前筛选条件下没有隔离记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="pager">
      <button class="btn" type="button" :disabled="page <= 1" data-testid="prev-page" @click="reload(page - 1)">
        上一页
      </button>
      <span data-testid="page-info">第 {{ page }} 页 / 共 {{ totalPages }} 页（每页 {{ size }} 条）</span>
      <button
        class="btn"
        type="button"
        :disabled="page >= totalPages"
        data-testid="next-page"
        @click="reload(page + 1)"
      >
        下一页
      </button>
    </footer>

    <!-- 提交结果：逐笔说明仍满足条件、曾经可重试现已完成、候选失效或待核验 -->
    <div v-if="lastSubmission" class="result-panel" data-testid="submission-result">
      <h3>重排申请 {{ lastSubmission.request_no }} 处理结论</h3>
      <p class="page-desc">{{ lastSubmission.message }}</p>
      <table class="data-table">
        <thead>
          <tr><th>样本编号</th><th>结论</th><th>说明</th><th>调度任务/回执</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in lastSubmission.results" :key="item.quarantine_id">
            <td>{{ item.样本编号 || `#${item.quarantine_id}` }}</td>
            <td><span class="badge" :class="badgeClass(item.state)">{{ item.state }}</span></td>
            <td>{{ item.reason }}</td>
            <td>
              <template v-if="item.task_id">{{ item.task_id }}（{{ item.回执 }}）</template>
              <template v-else-if="item.request_id">申请 #{{ item.request_id }} 等候核验</template>
              <template v-else>—</template>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 迟到回执：只有样本编号，调度确认后决定补采还是核验首份产物 -->
    <div class="action-panel">
      <h3>迟到成功回执处理</h3>
      <div class="filter-bar">
        <label class="filter-item">
          <span>样本编号（回执仅携带编号）</span>
          <input v-model="receiptSample" data-testid="receipt-sample" placeholder="如：E9" />
        </label>
        <button class="btn" type="button" data-testid="ingest-receipt" @click="ingest">登记回执</button>
        <button
          class="btn"
          type="button"
          data-testid="confirm-usable"
          :disabled="!receiptSample"
          @click="confirm(true)"
        >
          确认可用（核验首份产物，不补采）
        </button>
        <button
          class="btn"
          type="button"
          data-testid="confirm-unusable"
          :disabled="!receiptSample"
          @click="confirm(false)"
        >
          确认不可用（派发补采）
        </button>
      </div>
    </div>

    <!-- 计划页面：逐笔关联实际调度队列，核对来源、任务、回执与隔离项回填 -->
    <div class="action-panel">
      <h3>采集调度计划 · 恢复关联核对</h3>
      <div class="filter-bar">
        <label class="filter-item">
          <span>调度计划 ID</span>
          <input v-model.number="planId" data-testid="plan-id" type="number" min="1" placeholder="如：1" />
        </label>
        <button class="btn primary" type="button" data-testid="load-scope" @click="loadScope">
          核对计划恢复范围
        </button>
      </div>
      <div v-if="planScope" data-testid="plan-scope">
        <p class="page-desc">
          计划「{{ planScope.plan.计划名称 }}」（{{ planScope.plan.status }}）：
          已关联重排任务 {{ planScope.linked_total }} 笔，当前实际可恢复范围
          {{ planScope.recoverable_total }} 笔。
        </p>
        <table class="data-table">
          <thead>
            <tr>
              <th>任务编号</th><th>样本</th><th>真实来源</th><th>站点标识</th><th>失败阶段</th>
              <th>任务类型</th><th>回执</th><th>隔离项状态</th><th>隔离项处理方式</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="task in planScope.linked_tasks" :key="task.task_id">
              <td>{{ task.task_id }}</td>
              <td>{{ task.样本编号 }}</td>
              <td>{{ task.来源站点 }}</td>
              <td>{{ task.站点标识 }}</td>
              <td>{{ task.失败阶段 }}</td>
              <td>{{ task.任务类型 }}</td>
              <td>{{ task.回执 }}</td>
              <td>{{ task.隔离项状态 }}</td>
              <td>{{ task.隔离项处理方式 }}</td>
            </tr>
            <tr v-if="!planScope.linked_tasks.length">
              <td colspan="9" class="empty-state">该计划尚未关联重排任务</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <footer class="page-foot">
      <span v-if="message" class="error-text">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  changeStage,
  confirmReceipt,
  fetchCandidates,
  fetchPlanScope,
  ingestReceipt,
  submitReschedule,
  type CandidatePage,
  type PlanScope,
  type RescheduleResponse,
  type 候选状态,
} from '@/api/recovery'

const stages = ['采集', '解析', '计算', '匹配', '调度', '出口']
const size = 5

const filters = ref({ 来源站点: '站点B', 站点标识: '', 失败阶段: '采集' })
const page = ref(1)
const pageData = ref<CandidatePage>({
  ok: true,
  message: '',
  filters: { 来源站点: '', 站点标识: '', 失败阶段: '' },
  page: 1,
  size,
  total: 0,
  eligible_total: 0,
  items: [],
})
// 跨页选择保留候选身份，提交时由后端按当前数据重新校验，不信任旧快照。
const selectedIds = ref<Set<number>>(new Set())
const selectedSamples = ref<Map<number, string>>(new Map())
const submitting = ref(false)
const lastSubmission = ref<RescheduleResponse | null>(null)
const message = ref('')
const receiptSample = ref('E9')
const planId = ref<number | null>(1)
const planScope = ref<PlanScope | null>(null)

const totalPages = computed(() => Math.max(1, Math.ceil(pageData.value.total / size)))
const selectedSummary = computed(() =>
  Array.from(selectedSamples.value.entries())
    .filter(([id]) => selectedIds.value.has(id))
    .map(([, sample]) => sample)
    .join('、'),
)
const heldReceiptCount = computed(
  () => pageData.value.items.filter((item) => item.回执待确认).length,
)

function badgeClass(state: 候选状态 | string): string {
  if (state === '可重排' || state === '本次入选') return 'badge-ok'
  if (state === '待核验') return 'badge-held'
  if (state === '已处理') return 'badge-done'
  return 'badge-invalid'
}

function toggle(row: { id: number; 样本编号: string }) {
  const next = new Set(selectedIds.value)
  const samples = new Map(selectedSamples.value)
  if (next.has(row.id)) {
    next.delete(row.id)
    samples.delete(row.id)
  } else {
    next.add(row.id)
    samples.set(row.id, row.样本编号)
  }
  selectedIds.value = next
  selectedSamples.value = samples
}

function resetFilters() {
  filters.value = { 来源站点: '', 站点标识: '', 失败阶段: '' }
  void reload(1)
}

async function reload(targetPage: number) {
  message.value = ''
  page.value = targetPage
  try {
    pageData.value = await fetchCandidates({ ...filters.value, page: targetPage, size })
  } catch (error) {
    message.value = error instanceof Error ? error.message : '恢复候选读取失败'
  }
}

async function submit() {
  submitting.value = true
  message.value = ''
  try {
    lastSubmission.value = await submitReschedule({
      candidate_ids: Array.from(selectedIds.value),
      来源站点: filters.value.来源站点 || undefined,
      失败阶段: filters.value.失败阶段 || undefined,
    })
    selectedIds.value = new Set()
    selectedSamples.value = new Map()
    await reload(page.value)
  } catch (error) {
    message.value = error instanceof Error ? error.message : '重排提交失败'
  } finally {
    submitting.value = false
  }
}

async function ingest() {
  message.value = ''
  try {
    const result = await ingestReceipt(receiptSample.value.trim())
    message.value = result.message
    await reload(page.value)
  } catch (error) {
    message.value = error instanceof Error ? error.message : '回执登记失败'
  }
}

async function confirm(usable: boolean) {
  message.value = ''
  try {
    const result = await confirmReceipt(receiptSample.value.trim(), usable)
    message.value = result.message
    await reload(page.value)
  } catch (error) {
    message.value = error instanceof Error ? error.message : '回执确认失败'
  }
}

async function loadScope() {
  message.value = ''
  if (!planId.value) {
    message.value = '请输入调度计划 ID'
    return
  }
  try {
    planScope.value = await fetchPlanScope(planId.value)
  } catch (error) {
    message.value = error instanceof Error ? error.message : '计划恢复范围读取失败'
  }
}

// 暴露给控制台/联调：模拟翻页后同条记录重试阶段被后台推进。
defineExpose({ changeStage, reload })

onMounted(() => reload(1))
</script>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin: 10px 0;
  gap: 12px;
  flex-wrap: wrap;
}
.selection-hint { font-size: 13px; color: var(--muted); display: flex; gap: 8px; align-items: center; }
.recovery-table .col-check { width: 48px; text-align: center; }
.row-selected { background: #eff6ff; }
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  white-space: nowrap;
}
.badge-ok { background: #dcfce7; color: #166534; }
.badge-held { background: #fef9c3; color: #854d0e; }
.badge-done { background: #e2e8f0; color: #475569; }
.badge-invalid { background: #fee2e2; color: #991b1b; }
.cell-reason { display: block; font-size: 11px; color: var(--muted); margin-top: 2px; }
.pager { display: flex; gap: 12px; align-items: center; margin: 12px 0; font-size: 13px; }
.result-panel,
.action-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  margin-top: 16px;
}
.result-panel h3,
.action-panel h3 { margin: 0 0 10px; font-size: 15px; }
</style>
