import { createRouter, createWebHistory } from 'vue-router'
import OverviewView from './views/OverviewView.vue'
import InstancesView from './views/InstancesView.vue'
import TargetsView from './views/TargetsView.vue'
import JobsView from './views/JobsView.vue'
import SegmentsView from './views/SegmentsView.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: OverviewView },
    { path: '/instances', component: InstancesView },
    { path: '/targets', component: TargetsView },
    { path: '/jobs', component: JobsView },
    { path: '/segments', component: SegmentsView },
    { path: '/:rest(.*)', redirect: '/' },
  ],
})
