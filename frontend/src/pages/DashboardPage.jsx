import React, { useEffect, useRef, useState, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { BarChart3, Loader, ChevronRight, Send, Bot, X, Sparkles, ExternalLink, RefreshCw, Maximize } from 'lucide-react'
import api from '../services/api'
import s from './DashboardPage.module.css'


/* ──────────────────────────────────────────────────────────────────────
   Superset session helper — uses the backend server-side login endpoint.
   No credentials are ever sent or stored in the browser.
   ────────────────────────────────────────────────────────────────────── */
async function loginToSuperset(datasetId) {
  if (!datasetId) return false
  try {
    // The backend handles Superset authentication server-side and sets
    // a session cookie via its HTML auto-login page response.
    const resp = await fetch(`/api/dashboard/${datasetId}/superset-view`, {
      credentials: 'include',
      headers: {
        Authorization: `Bearer ${localStorage.getItem('dp_token') || ''}`,
      },
    })
    // A 200 means Superset already authenticated us (backend redirected via form)
    return resp.ok || resp.status === 0 || resp.redirected
  } catch (err) {
    console.warn('Superset session initialisation error:', err)
    return false
  }
}


/* ──────────────────────────────────────────────────────────────────────
   Dashboard Page — Superset iframe embedding
   ────────────────────────────────────────────────────────────────────── */
export default function DashboardPage() {
  const { datasetId } = useParams()
  const navigate = useNavigate()
  const iframeRef = useRef(null)
  const iframeContainerRef = useRef(null)

  const [dashboards, setDashboards] = useState([])
  const [selected, setSelected] = useState(null)
  const [loadingList, setLoadingList] = useState(true)
  const [iframeLoading, setIframeLoading] = useState(false)
  const [loggedIn, setLoggedIn] = useState(false)

  // Chat state
  const [chatOpen, setChatOpen] = useState(false)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const bottomRef = useRef(null)

  // ── Load dashboard list ──────────────────────────────────────────────
  useEffect(() => {
    api.get('/dashboard/')
      .then(r => {
        setDashboards(r.data)
        const target = datasetId
          ? r.data.find(d => d.dataset_id === datasetId)
          : r.data[0]
        if (target) setSelected(target)
      })
      .catch(() => {})
      .finally(() => setLoadingList(false))
  }, [datasetId])

  // ── Auto-login to Superset when selection changes ───────────────────
  useEffect(() => {
    if (!selected?.dataset_id) return
    loginToSuperset(selected.dataset_id).then(ok => setLoggedIn(ok))
  }, [selected?.dataset_id])

  // ── Load Superset dashboard in iframe ────────────────────────────────
  useEffect(() => {
    if (!selected || !loggedIn) return
    if (!selected.superset_dashboard_id) return

    setIframeLoading(true)
    const url = `/superset/dashboard/${selected.superset_dashboard_id}/?standalone=3`
    if (iframeRef.current) {
      iframeRef.current.src = url
    }
  }, [selected, loggedIn])

  const handleIframeLoad = useCallback(() => {
    setIframeLoading(false)
  }, [])

  // ── Chat scroll ─────────────────────────────────────────────────────
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // ── Refresh iframe ──────────────────────────────────────────────────
  const refreshDashboard = useCallback(() => {
    if (!selected?.superset_dashboard_id) return
    setIframeLoading(true)
    const url = `/superset/dashboard/${selected.superset_dashboard_id}/?standalone=3&t=${Date.now()}`
    if (iframeRef.current) iframeRef.current.src = url
  }, [selected])

  // ── Fullscreen toggle ───────────────────────────────────────────────
  const toggleFullScreen = useCallback(() => {
    if (!document.fullscreenElement) {
      iframeContainerRef.current?.requestFullscreen().catch(err => {
        console.warn(`Error attempting to enable fullscreen: ${err.message}`)
      })
    } else {
      document.exitFullscreen()
    }
  }, [])

  // ── Open in Superset (new tab) ──────────────────────────────────────
  const openInSuperset = useCallback(async () => {
    if (!selected) return
    // Ensure we are logged into Superset first
    await loginToSuperset()
    // Then simply open the dashboard in a new tab
    window.open(`/superset/dashboard/${selected.superset_dashboard_id}/?standalone=0`, '_blank')
  }, [selected])

  // ── Chat send ───────────────────────────────────────────────────────
  const sendChat = async (text) => {
    const msg = (text || input).trim()
    if (!msg || sending || !selected) return
    setInput('')
    setMessages(prev => [...prev, { role: 'user', content: msg }])
    setSending(true)
    const thinkId = Date.now()
    setMessages(prev => [...prev, { role: 'ai', thinking: true, id: thinkId }])
    try {
      const { data } = await api.post('/chat/query', {
        dataset_id: selected.dataset_id,
        message: msg,
        history: messages.slice(-6).map(m => ({
          role: m.role === 'user' ? 'user' : 'assistant',
          content: m.content || ''
        }))
      })
      setMessages(prev => prev.filter(m => m.id !== thinkId).concat({
        role: 'ai',
        content: data.answer,
        sql: data.sql_generated,
      }))
      // If a new chart was created in Superset, refresh the iframe
      if (data.new_chart_id) {
        setTimeout(() => refreshDashboard(), 2000)
      }
    } catch {
      setMessages(prev =>
        prev.filter(m => m.id !== thinkId)
            .concat({ role: 'ai', content: 'Sorry, something went wrong.' })
      )
    } finally {
      setSending(false)
    }
  }

  // ── Loading state ───────────────────────────────────────────────────
  if (loadingList) {
    return (
      <div className={s.loading}>
        <Loader size={18} className="spin" /> Loading dashboards...
      </div>
    )
  }

  // ── Empty state ─────────────────────────────────────────────────────
  if (!dashboards.length) {
    return (
      <div className={s.empty}>
        <BarChart3 size={40} className={s.emptyIcon} />
        <h3 className={s.emptyTitle}>No dashboards yet</h3>
        <p className={s.emptyDesc}>
          Upload a dataset or connect a database to generate your first AI dashboard
        </p>
        <button className={s.emptyBtn} onClick={() => navigate('/data-source')}>
          Get started <ChevronRight size={14} />
        </button>
      </div>
    )
  }

  // ── Main render ─────────────────────────────────────────────────────
  return (
    <div className={s.page + ' fade-in'}>
      {/* Sidebar */}
      <aside className={s.sidebar}>
        <div className={s.sidebarTitle}>Dashboards</div>
        {dashboards.map(d => (
          <button
            key={d.dataset_id}
            className={
              s.dsBtn + (selected?.dataset_id === d.dataset_id ? ' ' + s.dsBtnActive : '')
            }
            onClick={() => { setSelected(d); setMessages([]) }}
          >
            <span className={s.dsIcon}>
              {['csv', 'excel', 'sql_dump'].includes(d.source_type) ? '📄' : '🗄️'}
            </span>
            <div className={s.dsInfo}>
              <span className={s.dsName}>{d.dataset_name}</span>
              <span className={s.dsCharts}>{d.charts?.length || 0} charts</span>
            </div>
          </button>
        ))}
      </aside>

      {/* Center */}
      <div className={s.center}>
        {selected && (
          <>
            {/* Toolbar */}
            <div className={s.toolbar}>
              <div className={s.toolbarLeft}>
                <div className={s.dashBadge}>AI</div>
                <div>
                  <div className={s.dashTitle}>{selected.dataset_name}</div>
                  <div className={s.dashMeta}>
                    {selected.charts?.length || 0} charts · Superset Dashboard #{selected.superset_dashboard_id}
                  </div>
                </div>
              </div>
              <div className={s.toolbarActions}>
                <button className={s.refreshBtn} onClick={refreshDashboard} title="Refresh dashboard">
                  <RefreshCw size={13} />
                </button>
                <button className={s.refreshBtn} onClick={toggleFullScreen} title="Full Screen">
                  <Maximize size={13} />
                </button>
                <button className={s.supersetBtn} onClick={openInSuperset}>
                  <ExternalLink size={13} /> View in Superset
                </button>
                <button
                  className={s.aiBtn + (chatOpen ? ' ' + s.aiBtnActive : '')}
                  onClick={() => setChatOpen(o => !o)}
                >
                  <Sparkles size={13} /> AI Assistant
                </button>
              </div>
            </div>

            {/* Content: iframe + chat */}
            <div className={s.contentRow}>
              {/* Superset iframe */}
              <div className={s.iframeArea} ref={iframeContainerRef}>
                {iframeLoading && (
                  <div className={s.iframeOverlay}>
                    <Loader size={28} className="spin" />
                    <p>Loading Superset dashboard...</p>
                  </div>
                )}

                {!selected.superset_dashboard_id ? (
                  <div className={s.iframeOverlay}>
                    <BarChart3 size={32} style={{ color: '#CBD5E1' }} />
                    <p>No Superset dashboard linked to this dataset</p>
                  </div>
                ) : (
                  <iframe
                    ref={iframeRef}
                    className={s.supersetFrame}
                    title="Superset Dashboard"
                    onLoad={handleIframeLoad}
                  />
                )}
              </div>

              {/* Chat panel */}
              {chatOpen && (
                <div className={s.chatPanel}>
                  <div className={s.chatPanelHeader}>
                    <div className={s.chatPanelTitle}>
                      <div className={s.chatAvatar}><Bot size={14} color="#7EB8F7" /></div>
                      AI Assistant
                    </div>
                    <button className={s.closeBtn} onClick={() => setChatOpen(false)}>
                      <X size={14} />
                    </button>
                  </div>

                  <div className={s.chatMessages}>
                    {messages.length === 0 && (
                      <div className={s.chatWelcome}>
                        <p>Ask me anything about <strong>{selected.dataset_name}</strong></p>
                        <div className={s.suggestions}>
                          {[
                            'What are the key insights?',
                            'Show a bar chart of top apps',
                            'Compare usage by gender',
                            'Which device has highest usage?',
                          ].map(q => (
                            <button key={q} className={s.suggBtn} onClick={() => sendChat(q)}>
                              {q}
                            </button>
                          ))}
                        </div>
                      </div>
                    )}

                    {messages.map((m, i) => (
                      <div key={i} className={s.msg + ' ' + (m.role === 'user' ? s.msgUser : s.msgAi)}>
                        {m.thinking ? (
                          <div className={s.thinking}>
                            <span className={s.dot} /><span className={s.dot} /><span className={s.dot} />
                          </div>
                        ) : (
                          <>
                            <p className={s.msgText}>{m.content}</p>
                            {m.sql && (
                              <details className={s.sqlBlock}>
                                <summary>SQL</summary>
                                <pre>{m.sql}</pre>
                              </details>
                            )}
                          </>
                        )}
                      </div>
                    ))}
                    <div ref={bottomRef} />
                  </div>

                  <div className={s.chatInput}>
                    <input
                      value={input}
                      onChange={e => setInput(e.target.value)}
                      onKeyDown={e => e.key === 'Enter' && sendChat()}
                      placeholder="Ask about your data..."
                      disabled={sending}
                      className={s.inputField}
                    />
                    <button
                      className={s.sendBtn}
                      onClick={() => sendChat()}
                      disabled={!input.trim() || sending}
                    >
                      {sending ? <Loader size={13} className="spin" /> : <Send size={13} />}
                    </button>
                  </div>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}