<template>
  <section class="page" data-module="budget">
    <header class="page-head">
      <div>
        <h2>预算核算</h2>
        <p class="page-desc">读取预算条目、费用凭证与冲销记录，按最小货币单位分摊核算，生成预算执行差异数据集与科目对账附件。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="recover">核算恢复</button>
        <button class="btn" type="button" @click="correct">更正核算</button>
        <button class="btn primary" type="button" @click="runAccounting">执行核算</button>
        <button class="btn" type="button" @click="buildAttachment">生成对账附件</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <nav class="filter-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="btn"
        :class="{ primary: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <span v-if="message" class="page-desc">{{ message }}</span>
    <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>

    <!-- 预算执行差异数据集 -->
    <table v-if="activeTab === 'execution'" class="data-table">
      <thead>
        <tr>
          <th v-for="column in executionColumns" :key="column">{{ column }}</th>
          <th>分摊来源</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in executionRows" :key="`${row.预算周期}-${row.科目编号}-${row.部门}-${row.币种}`">
          <td v-for="column in executionColumns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span v-for="src in row.分摊来源" :key="`${src.凭证号}-${src.来源类型}`" class="tag">
              {{ src.凭证号 }} {{ src.来源类型 }} {{ src.金额 }}
            </span>
          </td>
        </tr>
        <tr v-if="!executionRows.length">
          <td :colspan="executionColumns.length + 1" class="empty-state">暂无执行差异数据，请先执行核算</td>
        </tr>
      </tbody>
    </table>

    <!-- 费用凭证与逐笔对账 -->
    <template v-if="activeTab === 'vouchers'">
      <table class="data-table">
        <thead>
          <tr>
            <th>凭证号</th><th>发生日期</th><th>币种</th><th>金额</th><th>状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in voucherRows" :key="String(row.id)">
            <td>{{ row.凭证号 }}</td>
            <td>{{ row.发生日期 }}</td>
            <td>{{ row.币种 }}</td>
            <td>{{ row.金额 }} {{ row.币种 }}</td>
            <td>{{ row.状态 }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="openReconciliation(row.id)">逐项对账</button>
            </td>
          </tr>
        </tbody>
      </table>

      <article v-if="reconciliation" class="stat-card" style="margin-top: 16px">
        <h3>对账详情：{{ reconciliation.凭证号 }}（{{ reconciliation.币种 }}）</h3>
        <p>原费用 {{ reconciliation.原费用 }} ｜ 分摊合计 {{ reconciliation.分摊合计 }} ｜ 已完成冲销 {{ reconciliation.周期内已完成冲销 }} ｜ 未完成 {{ reconciliation.未完成冲销 }} ｜ 剩余 {{ reconciliation.剩余金额 }}</p>
        <p :class="reconciliation.闭合校验.是否闭合 ? '' : 'error-text'">
          闭合校验：{{ reconciliation.闭合校验.是否闭合 ? '通过' : '未闭合' }}
          （分摊等于原费用：{{ reconciliation.闭合校验.分摊等于原费用 }}）
        </p>
        <table class="data-table">
          <thead><tr><th>科目</th><th>部门</th><th>比例</th><th>分摊金额</th><th>取整尾差</th></tr></thead>
          <tbody>
            <tr v-for="item in reconciliation.分摊明细" :key="`${item.科目编号}-${item.部门}`">
              <td>{{ item.科目编号 }}</td><td>{{ item.部门 }}</td><td>{{ item.分摊比例 }}</td>
              <td>{{ item.分摊金额 }}</td><td>{{ item.取整尾差 }}</td>
            </tr>
          </tbody>
        </table>
        <h4>冲销记录</h4>
        <table class="data-table">
          <thead><tr><th>冲销号</th><th>日期</th><th>状态</th><th>金额</th><th>跨期</th><th>计入实际</th></tr></thead>
          <tbody>
            <tr v-for="item in reconciliation.冲销记录" :key="item.冲销号">
              <td>{{ item.冲销号 }}</td><td>{{ item.发生日期 }}</td><td>{{ item.冲销状态 }}</td>
              <td>{{ item.金额 }}</td><td>{{ item.是否跨期 ? '是' : '否' }}</td>
              <td>{{ item.是否计入实际 ? '是' : '否' }}</td>
            </tr>
            <tr v-if="!reconciliation.冲销记录.length">
              <td colspan="6" class="empty-state">该凭证暂无冲销</td>
            </tr>
          </tbody>
        </table>
      </article>
    </template>

    <!-- 冲销记录 -->
    <table v-if="activeTab === 'reversals'" class="data-table">
      <thead>
        <tr><th>冲销号</th><th>原凭证号</th><th>发生日期</th><th>币种</th><th>金额</th><th>冲销状态</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in reversalRows" :key="String(row.id)">
          <td>{{ row.冲销号 }}</td><td>{{ row.原凭证号 }}</td><td>{{ row.发生日期 }}</td>
          <td>{{ row.币种 }}</td><td>{{ row.金额 }} {{ row.币种 }}</td><td>{{ row.冲销状态 }}</td>
        </tr>
        <tr v-if="!reversalRows.length">
          <td colspan="6" class="empty-state">暂无冲销记录</td>
        </tr>
      </tbody>
    </table>

    <!-- 异常行 -->
    <table v-if="activeTab === 'exceptions'" class="data-table">
      <thead>
        <tr><th>异常类型</th><th>来源环节</th><th>单据号</th><th>原因</th><th>发现时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in exceptionRows" :key="String(row.id)">
          <td>{{ row.异常类型 }}</td><td>{{ row.来源环节 }}</td><td>{{ row.单据号 }}</td>
          <td>{{ row.原因 }}</td><td>{{ row.发现时间 }}</td>
        </tr>
        <tr v-if="!exceptionRows.length">
          <td colspan="5" class="empty-state">暂无异常行</td>
        </tr>
      </tbody>
    </table>

    <!-- 核算依据版本 -->
    <table v-if="activeTab === 'bases'" class="data-table">
      <thead>
        <tr><th>依据编号</th><th>版本</th><th>触发类型</th><th>生成时间</th><th>状态</th><th>备注</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in basisRows" :key="String(row.id)">
          <td>{{ row.依据编号 }}</td><td>{{ row.版本 }}</td><td>{{ row.触发类型 }}</td>
          <td>{{ row.生成时间 }}</td><td>{{ row.状态 }}</td><td>{{ row.备注 }}</td>
        </tr>
        <tr v-if="!basisRows.length">
          <td colspan="6" class="empty-state">暂无核算依据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>预算核算数据以最小货币单位整数运算，不同币种分组汇总、互不相加</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

const ENDPOINT = '/api/finance/budget'

type Row = Record<string, any>

const tabs = [
  { key: 'execution', label: '执行差异数据集' },
  { key: 'vouchers', label: '费用凭证对账' },
  { key: 'reversals', label: '冲销记录' },
  { key: 'exceptions', label: '异常行' },
  { key: 'bases', label: '核算依据版本' },
]
const executionColumns = ['预算周期', '科目编号', '科目名称', '部门', '币种', '预算口径', '预算金额', '实际金额', '差额', '执行状态']

const activeTab = ref('execution')
const executionRows = ref<Row[]>([])
const voucherRows = ref<Row[]>([])
const reversalRows = ref<Row[]>([])
const exceptionRows = ref<Row[]>([])
const basisRows = ref<Row[]>([])
const reconciliation = ref<Row | null>(null)
const stats = ref([
  { label: '有效凭证', value: 0 },
  { label: '执行数据集行', value: 0 },
  { label: '异常行', value: 0 },
  { label: '核算依据版本', value: 0 },
])
const message = ref('')
const errorMessage = ref('')

async function getJson(path: string): Promise<any> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}`)
  }
  return response.json()
}

async function postAction(path: string, okText: string) {
  errorMessage.value = ''
  message.value = ''
  try {
    const response = await request(path, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.message || payload.detail || '操作未生效')
    }
    message.value = payload.message || okText
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预算核算操作失败'
  }
}

function runAccounting() {
  return postAction(`${ENDPOINT}/accounting/run`, '核算完成')
}
function recover() {
  return postAction(`${ENDPOINT}/accounting/recover`, '核算恢复完成')
}
function correct() {
  return postAction(`${ENDPOINT}/accounting/correct`, '更正核算完成')
}
function buildAttachment() {
  return postAction(`${ENDPOINT}/attachments`, '对账附件已生成')
}

async function openReconciliation(voucherId: number) {
  errorMessage.value = ''
  try {
    reconciliation.value = await getJson(`${ENDPOINT}/vouchers/${voucherId}/reconciliation`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对账数据读取失败'
  }
}

async function switchTab(key: string) {
  activeTab.value = key
  reconciliation.value = null
  await reload()
}

async function reload() {
  errorMessage.value = ''
  try {
    const [execution, vouchers, reversals, exceptions, bases] = await Promise.all([
      getJson(`${ENDPOINT}/execution`),
      getJson(`${ENDPOINT}/vouchers?size=200`),
      getJson(`${ENDPOINT}/reversals`),
      getJson(`${ENDPOINT}/exceptions`),
      getJson(`${ENDPOINT}/bases`),
    ])
    executionRows.value = execution.items ?? []
    voucherRows.value = vouchers.items ?? []
    reversalRows.value = reversals.items ?? []
    exceptionRows.value = exceptions.items ?? []
    basisRows.value = bases.items ?? []
    stats.value = [
      { label: '有效凭证', value: voucherRows.value.length },
      { label: '执行数据集行', value: executionRows.value.length },
      { label: '异常行', value: exceptionRows.value.length },
      { label: '核算依据版本', value: basisRows.value.length },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预算核算数据读取失败'
  }
}

onMounted(reload)
</script>
