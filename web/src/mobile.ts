import { onMounted, onUnmounted, ref } from 'vue'

const QUERY = '(max-width: 768px)'

export function useMobile() {
  const mobile = ref(typeof window !== 'undefined' && !!window.matchMedia && window.matchMedia(QUERY).matches)
  let media: MediaQueryList | null = null
  const update = () => {
    mobile.value = !!media?.matches
  }
  onMounted(() => {
    if (!window.matchMedia) return
    media = window.matchMedia(QUERY)
    update()
    media.addEventListener('change', update)
  })
  onUnmounted(() => media?.removeEventListener('change', update))
  return mobile
}
