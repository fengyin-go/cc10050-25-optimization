import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import RecoveryView from '@/views/RecoveryView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 失败隔离区承载恢复候选筛选、重排申请、迟到回执与计划关联的完整链路，
    // 使用专用工作台；其余模块继续走通用资源工作台。
    {
      path: '/failure_quarantine',
      name: 'failure_quarantine',
      component: RecoveryView,
      props: { moduleKey: 'failure_quarantine' },
    },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
