import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import QualityFlags from '../src/components/QualityFlags.vue'
import type { QualityFlag } from '../src/api/types'

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

  it('同一切片多次出现相同标记时聚合成单个标签并显示次数', () => {
    const wrapper = render([
      { flag: 'low_frame_rate', suspected_start_time: 1794807990 },
      { flag: 'low_frame_rate', suspected_start_time: 1794808176 },
      { flag: 'low_frame_rate', suspected_start_time: 1794808281 },
    ])
    const tags = wrapper.findAll('.el-tag')
    expect(tags.length).toBe(1)
    expect(tags[0]!.text()).toBe('低帧率 × 3')
  })

  it('不同标记各自保留一个标签并按出现顺序展示', () => {
    const wrapper = render([
      { flag: 'low_frame_rate', suspected_start_time: 1794807990 },
      { flag: 'overlay_live_panel_window', suspected_start_time: 1794808000 },
      { flag: 'low_frame_rate', suspected_start_time: 1794808176 },
    ])
    const tags = wrapper.findAll('.el-tag')
    expect(tags.length).toBe(2)
    expect(tags[0]!.text()).toBe('低帧率 × 2')
    expect(tags[1]!.text()).toBe('遮挡：直播面板')
  })
})
