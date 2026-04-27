import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { ArrowUp, Loader2, MessageSquare, Wrench, X } from 'lucide-react'
import { api } from '@/lib/api'
import type { ChatEvent } from '@/lib/types'
import { cn } from '@/lib/utils'

type ToolCall = { name: string; input: Record<string, unknown> }
type Message =
  | { role: 'user'; text: string; id: string }
  | { role: 'assistant'; text: string; tools: ToolCall[]; id: string; pending?: boolean }

const SUGGESTIONS = [
  'Should I run hard today?',
  'Why is my fitness slipping?',
  'How\'s my sleep been lately?',
  'What was my best stretch of training this year?',
]

const newId = () => Math.random().toString(36).slice(2, 10)

type SeedRequest = { text: string; nonce: number }

export function ChatPanel({ seedRequest }: { seedRequest?: SeedRequest | null }) {
  const [sessionId] = useState(() => crypto.randomUUID())
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [model, setModel] = useState<'sonnet' | 'opus'>('sonnet')
  const [streaming, setStreaming] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const lastMsgRef = useRef<HTMLDivElement>(null)

  // Close session on unmount
  useEffect(() => {
    return () => {
      api.chatEnd(sessionId).catch(() => {})
    }
  }, [sessionId])

  // External seeding — when a TakeawayCard "Ask about this" is clicked,
  // the parent bumps a nonce so we re-fire even if the seed text is
  // identical. We don't auto-send; user gets a chance to edit first.
  useEffect(() => {
    if (!seedRequest) return
    setInput(seedRequest.text)
    requestAnimationFrame(() => {
      const ta = taRef.current
      if (!ta) return
      ta.focus()
      ta.setSelectionRange(ta.value.length, ta.value.length)
      ta.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
  }, [seedRequest])

  // Scroll the page so the latest message is visible without yanking the
  // brief out of view — only scroll if the latest message is below the fold.
  useEffect(() => {
    if (messages.length === 0) return
    lastMsgRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [messages])

  async function send(text: string) {
    const trimmed = text.trim()
    if (!trimmed || streaming) return

    const modelId = model === 'opus' ? 'claude-opus-4-7' : 'claude-sonnet-4-6'
    const userMsg: Message = { role: 'user', text: trimmed, id: newId() }
    const assistantMsg: Message = {
      role: 'assistant', text: '', tools: [], id: newId(), pending: true,
    }
    setMessages((m) => [...m, userMsg, assistantMsg])
    setInput('')
    setStreaming(true)
    abortRef.current = new AbortController()

    try {
      for await (const ev of api.chat(sessionId, trimmed, modelId, abortRef.current.signal)) {
        applyEvent(assistantMsg.id, ev)
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      applyEvent(assistantMsg.id, { type: 'error', message: msg })
    } finally {
      setMessages((m) => m.map((x) => (x.id === assistantMsg.id ? { ...x, pending: false } as Message : x)))
      setStreaming(false)
      abortRef.current = null
    }
  }

  function applyEvent(assistantId: string, ev: ChatEvent) {
    setMessages((m) =>
      m.map((msg) => {
        if (msg.id !== assistantId || msg.role !== 'assistant') return msg
        if (ev.type === 'text') return { ...msg, text: msg.text + ev.text }
        if (ev.type === 'tool_use') return { ...msg, tools: [...msg.tools, { name: ev.name, input: ev.input }] }
        if (ev.type === 'error') return { ...msg, text: (msg.text ? msg.text + '\n\n' : '') + `⚠ ${ev.message}` }
        return msg
      }),
    )
  }

  function clearConversation() {
    abortRef.current?.abort()
    api.chatEnd(sessionId).catch(() => {})
    setMessages([])
  }

  function onKey(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send(input)
    }
  }

  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wider text-muted">
          <MessageSquare className="size-3.5 text-accent" />
          Ask
        </div>
        <div className="flex items-center gap-2">
          <ModelToggle value={model} onChange={setModel} />
          {messages.length > 0 && (
            <button
              onClick={clearConversation}
              className="text-[11px] text-muted hover:text-text px-2 py-1 rounded inline-flex items-center gap-1"
              title="Clear conversation"
            >
              <X className="size-3" />
              Clear
            </button>
          )}
        </div>
      </div>

      {/* Composer always-visible */}
      <form
        onSubmit={(e) => { e.preventDefault(); send(input) }}
        className="flex items-end gap-2 bg-surface border border-border rounded-2xl px-4 py-3 focus-within:border-accent-dim transition-colors"
      >
        <textarea
          ref={taRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKey}
          placeholder="Ask a follow-up about today's brief or any pattern in your data…"
          rows={1}
          className="flex-1 bg-transparent resize-none outline-none placeholder:text-faint text-[15px] leading-relaxed max-h-48"
        />
        <button
          type="submit"
          disabled={!input.trim() || streaming}
          className={cn(
            'size-8 shrink-0 rounded-full flex items-center justify-center transition-colors',
            input.trim() && !streaming
              ? 'bg-accent text-bg hover:opacity-90'
              : 'bg-surface-2 text-faint cursor-not-allowed',
          )}
        >
          {streaming ? <Loader2 className="size-4 animate-spin" /> : <ArrowUp className="size-4" />}
        </button>
      </form>

      {/* Suggestions when empty, conversation when not */}
      {messages.length === 0 ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              onClick={() => { setInput(s); taRef.current?.focus() }}
              className="text-left text-sm px-4 py-2.5 bg-surface/60 border border-border rounded-xl text-muted hover:text-text hover:bg-surface hover:border-accent-dim transition-colors"
            >
              {s}
            </button>
          ))}
        </div>
      ) : (
        <div className="space-y-5 pt-2">
          {messages.map((m, i) => (
            <div key={m.id} ref={i === messages.length - 1 ? lastMsgRef : null}>
              <MessageBubble message={m} streaming={streaming} />
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function MessageBubble({ message, streaming }: { message: Message; streaming: boolean }) {
  if (message.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] bg-surface border border-border rounded-2xl px-4 py-2.5 text-[15px]">
          {message.text}
        </div>
      </div>
    )
  }
  const isPending = message.pending && streaming
  return (
    <div>
      {message.tools.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {message.tools.map((t, i) => (
            <span
              key={i}
              className="inline-flex items-center gap-1.5 text-[11px] text-muted bg-surface px-2 py-1 rounded-full border border-border"
              title={JSON.stringify(t.input)}
            >
              <Wrench className="size-3" />
              {t.name.replace(/^mcp__fitness__/, '')}
            </span>
          ))}
        </div>
      )}
      <div className="prose-fitness text-[15px]">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.text || ''}</ReactMarkdown>
        {isPending && !message.text && (
          <div className="text-muted text-sm flex items-center gap-2">
            <Loader2 className="size-3.5 animate-spin" />
            Thinking…
          </div>
        )}
        {isPending && message.text && (
          <span className="inline-block size-2 rounded-full bg-accent ml-0.5 animate-pulse" />
        )}
      </div>
    </div>
  )
}

function ModelToggle({ value, onChange }: { value: 'sonnet' | 'opus'; onChange: (v: 'sonnet' | 'opus') => void }) {
  return (
    <div className="flex bg-surface border border-border rounded-full p-0.5 text-[11px]">
      {(['sonnet', 'opus'] as const).map((m) => (
        <button
          key={m}
          onClick={() => onChange(m)}
          className={cn(
            'px-2.5 py-0.5 rounded-full transition-colors',
            value === m ? 'bg-accent text-bg' : 'text-muted hover:text-text',
          )}
        >
          {m === 'sonnet' ? 'Sonnet' : 'Opus'}
        </button>
      ))}
    </div>
  )
}
