<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>运营概览</h2>
        <p class="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
      </div>
    </header>
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in moduleRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.created }}</td>
          <td>{{ row.pending }}</td>
          <td>{{ row.abnormal }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

const cards = ref<Overview['cards']>([])
const moduleRows = ref<Overview['modules']>([])

onMounted(async () => {
  try {
    const payload = await fetchJson<Overview>('/api/overview')
    cards.value = payload.cards
    moduleRows.value = payload.modules
  } catch {
    cards.value = [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}]
    moduleRows.value = [{"name": "经营指标看板", "created": 0, "pending": 0, "abnormal": 0}, {"name": "财务多维分析", "created": 0, "pending": 0, "abnormal": 0}, {"name": "销售漏斗洞察", "created": 0, "pending": 0, "abnormal": 0}, {"name": "客户画像构建", "created": 0, "pending": 0, "abnormal": 0}, {"name": "供应链效率监控", "created": 0, "pending": 0, "abnormal": 0}, {"name": "人力效能评估", "created": 0, "pending": 0, "abnormal": 0}, {"name": "经营预测模型", "created": 0, "pending": 0, "abnormal": 0}, {"name": "异常预警中心", "created": 0, "pending": 0, "abnormal": 0}, {"name": "决策建议推送", "created": 0, "pending": 0, "abnormal": 0}, {"name": "多维下钻分析", "created": 0, "pending": 0, "abnormal": 0}, {"name": "经营分析报告", "created": 0, "pending": 0, "abnormal": 0}, {"name": "同期对比分析", "created": 0, "pending": 0, "abnormal": 0}, {"name": "分析数据接入", "created": 0, "pending": 0, "abnormal": 0}, {"name": "分析缓存管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "分析模板库", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据刷新调度", "created": 0, "pending": 0, "abnormal": 0}]
  }
})
</script>
