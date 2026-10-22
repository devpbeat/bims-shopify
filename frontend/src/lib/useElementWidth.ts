import { useCallback, useState } from 'react'

/** Tracks the content width of an element; returns [callbackRef, width]. */
export function useElementWidth(fallback: number): [(node: HTMLElement | null) => void, number] {
  const [width, setWidth] = useState(fallback)
  const ref = useCallback((node: HTMLElement | null) => {
    if (!node) return
    setWidth(Math.round(node.getBoundingClientRect().width) || fallback)
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setWidth(Math.round(entry.contentRect.width) || fallback)
    })
    observer.observe(node)
    // React 19 runs the returned function when the ref detaches.
    return () => observer.disconnect()
  }, [fallback])
  return [ref, width]
}
