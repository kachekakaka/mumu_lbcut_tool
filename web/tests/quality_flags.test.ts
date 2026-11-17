import { afterEach, describe, expect, it } from 'vitest'
import { enableAutoUnmount, mount } from '@vue/test-utils'
import ElementPlus, { ElTooltip } from 'element-plus'
import QualityFlags from '../src/components/QualityFlags.vue'
import type { QualityFlag } from '../src/api/types'
import { fmtTime } from '../src/format'

enableAutoUnmount(afterEach)

describe('QualityFlags 质量标记聚合与展示', () => {
  function render(flags: QualityFlag[]) {
    return mount(QualityFlags, {
      props: { flags },
      global: { plugins: [ElementPlus] },
    })
  }

  it('无标记时显示横杠', () => {
    const wrapper = render([])
    expect(wrapper.text()).toBe('—')
  })

  it('单个标记显示中文名称，无倍数后缀', () => {
    const wrapper = render([
      { flag: 'low_frame_rate', suspected_start_time: 1794807934 },
    ])
    const tag = wrapper.find('.el-tag')
    expect(tag.exists()).toBe(true)
    expect(tag.text()).toBe('低帧率')
  })

  it('同一切片多个同名标记显示记录条数及疑似时间范围', () => {
    const wrapper = render([
      { flag: 'low_frame_rate', suspected_start_time: 1794807990 },
      { flag: 'low_frame_rate', suspected_start_time: 1794808176 },
      { flag: 'low_frame_rate', suspected_start_time: 1794808281 },
    ])
    const tags = wrapper.findAll('.el-tag')
    expect(tags.length).toBe(1)
    expect(tags[0]!.text()).toBe('低帧率 · 3 条')
    expect(wrapper.findComponent(ElTooltip).props('content')).toBe(
      `标记 3 条；最早疑似开始：${fmtTime(1794807990)}；最晚疑似开始：${fmtTime(1794808281)}（low_frame_rate）`,
    )
  })

  it('不同标记各自保留一个标签并按出现顺序展示', () => {
    const wrapper = render([
      { flag: 'low_frame_rate', suspected_start_time: 1794807990 },
      { flag: 'overlay_live_panel_window', suspected_start_time: 1794808000 },
      { flag: 'low_frame_rate', suspected_start_time: 1794808176 },
    ])
    const tags = wrapper.findAll('.el-tag')
    expect(tags.length).toBe(2)
    expect(tags[0]!.text()).toBe('低帧率 · 2 条')
    expect(tags[1]!.text()).toBe('遮挡：直播面板')
  })

  it('内容相同的记录不再去重，零值保留，未知时间单独计数', async () => {
    const flag = { flag: 'low_frame_rate', suspected_start_time: 0 }
    const wrapper = render([flag, flag, { ...flag, suspected_start_time: null }])
    expect(wrapper.find('.el-tag').text()).toBe('低帧率 · 3 条')
    expect(wrapper.findComponent(ElTooltip).props('content')).toBe(
      `标记 3 条；疑似开始：${fmtTime(0)}；另有 1 条疑似开始时间未知（low_frame_rate）`,
    )
    // 刷新替换完整列表，不在组件内累积旧结果。
    await wrapper.setProps({ flags: [flag, flag] })
    expect(wrapper.find('.el-tag').text()).toBe('低帧率 · 2 条')
  })

  it('非有限时间按未知处理，null 与未知字符串名称不碰撞', () => {
    const wrapper = render([
      { flag: null, suspected_start_time: null },
      { flag: '__null__', suspected_start_time: Number.NaN },
      { flag: '__null__', suspected_start_time: Infinity },
    ])
    expect(wrapper.findAll('.el-tag').map(t => t.text())).toEqual(['未知标记', '__null__ · 2 条'])
    expect(wrapper.findAllComponents(ElTooltip).map(t => t.props('content'))).toEqual([
      '标记 1 条；疑似开始时间未知', '标记 2 条；疑似开始时间未知（__null__）',
    ])
  })
})
