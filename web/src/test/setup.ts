// Registers @testing-library/jest-dom matchers (toBeInTheDocument, etc.) and
// augments vitest's `expect` with their types.
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach } from 'vitest'
import { cleanup } from '@testing-library/react'

// Node 25 ships an experimental global `localStorage` (gated behind
// --localstorage-file) that shadows jsdom's Storage on `window`, leaving a stub
// with no getItem/setItem/clear. The app reads window.localStorage directly, so
// install a clean, deterministic in-memory Storage for tests.
class MemoryStorage implements Storage {
  private store = new Map<string, string>()
  get length(): number {
    return this.store.size
  }
  clear(): void {
    this.store.clear()
  }
  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key)! : null
  }
  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null
  }
  removeItem(key: string): void {
    this.store.delete(key)
  }
  setItem(key: string, value: string): void {
    this.store.set(key, String(value))
  }
}

Object.defineProperty(window, 'localStorage', {
  configurable: true,
  value: new MemoryStorage(),
})

beforeEach(() => {
  window.localStorage.clear()
})

// Unmount React trees between tests so the AuthGate's onAuthRequired listener
// from one test can't fire into another.
afterEach(() => {
  cleanup()
})
