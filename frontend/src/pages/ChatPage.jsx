import React, { useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { Send, Loader, Bot, User, BarChart3, Sparkles } from 'lucide-react'
import api from '../services/api'
import s from './ChatPage.module.css'

const SUGGESTIONS = [
  'Show me the overall revenue trend',
  'Which category has the highest sales?',
  'Create a bar chart comparing regions',
  'What are the top 5 products by revenue?',
  'Show monthly sales as a line chart',
  'Compare performance quarter over quarter',
]

export default function ChatPage() {
  const { datasetId: paramDatasetId } = useParams()
  const [datasets, setDatasets] = useState([])
  const [selectedId, setSelectedId] = useState(paramDatasetId || null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const bottomRef = useRef(null)
  const inputRef = useRef(null)

  // Load ready datasets
  useEffect(() => {
    api.get('/ingest/datasets').then(r => {
      const ready = r.data.filter(d => d.status === 'superset_ready')
      setDatasets(ready)
      if (!selectedId && ready.length) setSelectedId(ready[0].id)
    }).catch(() => {})
  }, [])

  // Scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const selectedDataset = datasets.find(d => d.id === selectedId)

  const sendMessage = async (text) => {
    const msg = (text || input).trim()
    if (!msg || sending || !selectedId) return

    setInput('')
    const userMsg = { id: Date.now(), role: 'user', content: msg }
    setMessages(prev => [...prev, userMsg])
    setSending(true)

    // Optimistic thinking indicator
    const thinkingId = Date.now() + 1
    setMessages(prev => [...prev, { id: thinkingId, role: 'assistant', thinking: true }])

    try {
      const history = messages.slice(-8).map(m => ({
        role: m.role === 'user' ? 'user' : 'assistant',
        content: m.content || '',
      }))

      const { data } = await api.post('/chat/query', {
        dataset_id: selectedId,
        message: msg,
        history,
      })

      setMessages(prev => prev.filter(m => m.id !== thinkingId).concat({
        id: Date.now() + 2,
        role: 'assistant',
        content: data.answer,
        sql: data.sql_generated,
        newChartId: data.new_chart_id,
        modifiedChartId: data.modified_chart_id,
        sources: data.sources || [],
      }))
    } catch {
      setMessages(prev => prev.filter(m => m.id !== thinkingId).concat({
        id: Date.now() + 2,
        role: 'assistant',
        content: 'Sorry, I encountered an error processing your request. Please try again.',
        error: true,
      }))
    } finally {
      setSending(false)
      inputRef.current?.focus()
    }
  }

  const handleKey = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage() }
  }

  return (
    <div className={`${s.page} fade-in`}>
      {/* Dataset selector sidebar */}
      <aside className={s.sidebar}>
        <div className={s.sidebarTitle}>Dataset</div>
        {datasets.length === 0 && (
          <p className={s.noDatasets}>No ready datasets yet. Process a dataset first.</p>
        )}
        {datasets.map(d => (
          <button
            key={d.id}
            className={`${s.dsBtn} ${selectedId === d.id ? s.dsBtnActive : ''}`}
            onClick={() => { setSelectedId(d.id); setMessages([]) }}
          >
            <span className={s.dsIcon}>{['csv','excel','sql_dump'].includes(d.source_type) ? '📄' : '🗄️'}</span>
            <span className={s.dsName}>{d.name}</span>
          </button>
        ))}

        <div className={s.ragNote}>
          <Sparkles size={12} />
          <div>
            <div className={s.ragTitle}>100% RAG powered</div>
            <div className={s.ragDesc}>Every answer is retrieved directly from your data — no static responses</div>
          </div>
        </div>

        <div className={s.sidebarTitle} style={{ marginTop: 16 }}>Try asking</div>
        <div className={s.suggList}>
          {SUGGESTIONS.map(suggestion => (
            <button key={suggestion} className={s.suggBtn} onClick={() => sendMessage(suggestion)}
              style={{ width:'100%', textAlign:'left', background:'none', border:'1px solid var(--border)',
                borderRadius:'var(--radius-md)', padding:'6px 10px', fontSize:11, color:'var(--gray-600)',
                cursor:'pointer', transition:'all var(--transition)', marginBottom:4 }}
              onMouseOver={e => { e.currentTarget.style.background='var(--surface-2)'; e.currentTarget.style.color='var(--gray-900)' }}
              onMouseOut={e => { e.currentTarget.style.background='none'; e.currentTarget.style.color='var(--gray-600)' }}
            >{suggestion}</button>
          ))}
        </div>
      </aside>

      {/* Chat panel */}
      <div className={s.chat}>
        {/* Header */}
        <div className={s.chatHeader}>
          <div className={s.chatHeaderLeft}>
            <div className={s.botAvatar}><Bot size={16} color="#7EB8F7" /></div>
            <div>
              <div className={s.chatTitle}>AI Analytics Assistant</div>
              <div className={s.chatSub}>
                {selectedDataset
                  ? `Connected to ${selectedDataset.name}`
                  : 'Select a dataset to begin'}
              </div>
            </div>
          </div>
          <div className={s.ragBadge}><Sparkles size={10} /> RAG · Gemini 2.0 Flash</div>
        </div>

        {/* Messages */}
        <div className={s.messages}>
          {messages.length === 0 && (
            <div className={s.welcome}>
              <div className={s.welcomeIcon}><Bot size={28} /></div>
              <h3 className={s.welcomeTitle}>Ask anything about your data</h3>
              <p className={s.welcomeDesc}>
                I retrieve real answers from <strong>{selectedDataset?.name || 'your dataset'}</strong>.
                Ask for charts, insights, comparisons, or trends — I'll generate SQL and visualizations on the fly.
              </p>
              <div className={s.chipGrid}>
                {SUGGESTIONS.slice(0, 4).map(sugg => (
                  <button key={sugg} className={s.chip} onClick={() => sendMessage(sugg)}>
                    {sugg}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map(msg => (
            <div key={msg.id} className={`${s.msgRow} ${msg.role === 'user' ? s.msgRowUser : ''}`}>
              <div className={`${s.msgAvatar} ${msg.role === 'user' ? s.msgAvatarUser : s.msgAvatarBot}`}>
                {msg.role === 'user' ? <User size={13} /> : <Bot size={13} />}
              </div>
              <div className={`${s.bubble} ${msg.role === 'user' ? s.bubbleUser : s.bubbleBot} ${msg.error ? s.bubbleError : ''}`}>
                {msg.thinking ? (
                  <div className={s.thinking}>
                    <span className={s.dot} /><span className={s.dot} /><span className={s.dot} />
                  </div>
                ) : (
                  <>
                    <p className={s.msgText}>{msg.content}</p>

                    {msg.sql && (
                      <details className={s.sqlBlock}>
                        <summary className={s.sqlSummary}>SQL generated</summary>
                        <pre className={s.sqlCode}>{msg.sql}</pre>
                      </details>
                    )}

                    {(msg.newChartId || msg.modifiedChartId) && (
                      <div className={s.chartNotice}>
                        <BarChart3 size={12} />
                        {msg.newChartId ? 'New chart created and added to your dashboard' : 'Chart updated on your dashboard'}
                      </div>
                    )}

                    {msg.sources?.length > 0 && (
                      <details className={s.sourcesBlock}>
                        <summary className={s.sourcesSummary}>Data sources ({msg.sources.length})</summary>
                        {msg.sources.map((src, i) => (
                          <div key={i} className={s.sourceChunk}>{src}</div>
                        ))}
                      </details>
                    )}
                  </>
                )}
              </div>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>

        {/* Input */}
        <div className={s.inputArea}>
          <textarea
            ref={inputRef}
            className={s.input}
            rows={1}
            placeholder={selectedId ? 'Ask about your data, request a chart, compare metrics…' : 'Select a dataset to start chatting'}
            value={input}
            disabled={!selectedId || sending}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKey}
          />
          <button
            className={s.sendBtn}
            disabled={!input.trim() || sending || !selectedId}
            onClick={() => sendMessage()}
          >
            {sending ? <Loader size={15} className="spin" /> : <Send size={15} />}
          </button>
        </div>
        <p className={s.disclaimer}>Answers are generated from your actual data via Gemini AI + RAG retrieval</p>
      </div>
    </div>
  )
}
