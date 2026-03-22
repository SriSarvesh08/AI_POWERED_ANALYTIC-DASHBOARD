import React, { useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useDropzone } from 'react-dropzone'
import { Upload, Database, ChevronRight, CheckCircle, AlertCircle, Loader } from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../services/api'
import s from './DataSourcePage.module.css'

const DB_TYPES = [
  { id: 'postgresql', label: 'PostgreSQL', emoji: '🐘', defaultPort: 5432 },
  { id: 'mysql',      label: 'MySQL',      emoji: '🐬', defaultPort: 3306 },
  { id: 'snowflake',  label: 'Snowflake',  emoji: '❄️', defaultPort: 443 },
  { id: 'firebase',   label: 'Firebase',   emoji: '🔥', defaultPort: null },
]

export default function DataSourcePage() {
  const navigate = useNavigate()
  const [mode, setMode] = useState('upload')        // 'upload' | 'connect'
  const [uploading, setUploading] = useState(false)
  const [selectedDB, setSelectedDB] = useState(null)
  const [connForm, setConnForm] = useState({ alias:'My Database', host:'', port:'', database:'', username:'', password:'', account:'', warehouse:'', schema:'', service_account_json:'', project_id:'' })
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState(null)  // null | 'ok' | 'error'
  const [connecting, setConnecting] = useState(false)

  /* ── File upload ──────────────────────────────────────────────────────── */
  const onDrop = useCallback(async (accepted) => {
    if (!accepted.length) return
    const file = accepted[0]
    const ext = file.name.split('.').pop().toLowerCase()
    if (!['csv','xlsx','xls','sql'].includes(ext)) {
      toast.error('Unsupported file type. Use CSV, Excel, or SQL dump.')
      return
    }
    setUploading(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      const { data } = await api.post('/ingest/upload', fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      toast.success('File uploaded — AI pipeline started!')
      navigate(`/pipeline/${data.id}`)
    } catch {
      toast.error('Upload failed')
    } finally {
      setUploading(false)
    }
  }, [navigate])

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    multiple: false,
    accept: { 'text/csv': ['.csv'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ['.xlsx'], 'application/vnd.ms-excel': ['.xls'], 'application/sql': ['.sql'], 'text/plain': ['.sql'] },
  })

  /* ── DB connect ───────────────────────────────────────────────────────── */
  const handleSelectDB = (db) => {
    setSelectedDB(db)
    setConnForm(f => ({ ...f, port: db.defaultPort?.toString() || '' }))
    setTestResult(null)
  }

  const handleTest = async () => {
    if (!selectedDB) { toast.error('Select a database type first'); return }
    setTesting(true)
    setTestResult(null)
    try {
      await api.post('/connect/test', buildPayload())
      setTestResult('ok')
    } catch {
      setTestResult('error')
    } finally {
      setTesting(false)
    }
  }

  const handleConnect = async () => {
    if (!selectedDB) { toast.error('Select a database type first'); return }
    setConnecting(true)
    try {
      const { data } = await api.post('/connect/connect', buildPayload())
      toast.success('Connected — AI pipeline started!')
      navigate(`/pipeline/${data.id}`)
    } catch {
      toast.error('Connection failed')
    } finally {
      setConnecting(false)
    }
  }

  const buildPayload = () => ({
    db_type: selectedDB.id,
    alias: connForm.alias || selectedDB.label,
    host: connForm.host || undefined,
    port: connForm.port ? parseInt(connForm.port) : undefined,
    database: connForm.database || undefined,
    username: connForm.username || undefined,
    password: connForm.password || undefined,
    account: connForm.account || undefined,
    warehouse: connForm.warehouse || undefined,
    schema: connForm.schema || undefined,
    service_account_json: connForm.service_account_json || undefined,
    project_id: connForm.project_id || undefined,
  })

  const set = (k) => (e) => setConnForm(f => ({ ...f, [k]: e.target.value }))

  const handleFirebaseUpload = (e) => {
    const file = e.target.files[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = (event) => {
      try {
        const jsonContent = event.target.result
        const json = JSON.parse(jsonContent)
        setConnForm(f => ({
          ...f,
          service_account_json: jsonContent,
          project_id: json.project_id || f.project_id
        }))
        toast.success('Firebase JSON loaded!')
      } catch (err) {
        toast.error('Invalid JSON file')
      }
    }
    reader.readAsText(file)
  }

  return (
    <div className="fade-in">
      <div className={s.header}>
        <h2 className={s.title}>Data source</h2>
        <p className={s.sub}>Upload a dataset or connect an existing database — AI handles the rest</p>
      </div>

      {/* Mode toggle */}
      <div className={s.modeRow}>
        <button className={`${s.modeBtn} ${mode==='upload'?s.modeBtnActive:''}`} onClick={() => setMode('upload')}>
          <Upload size={14} /> Upload dataset
        </button>
        <button className={`${s.modeBtn} ${mode==='connect'?s.modeBtnActive:''}`} onClick={() => setMode('connect')}>
          <Database size={14} /> Connect database
        </button>
      </div>

      {/* ── Upload mode ──────────────────────────────────────────────────── */}
      {mode === 'upload' && (
        <div className={s.grid}>
          <div>
            <div {...getRootProps()} className={`${s.dropzone} ${isDragActive ? s.dropzoneActive : ''} ${uploading ? s.dropzoneUploading : ''}`}>
              <input {...getInputProps()} />
              {uploading ? (
                <>
                  <Loader size={32} className={`${s.dzIcon} spin`} />
                  <p className={s.dzTitle}>Uploading…</p>
                </>
              ) : (
                <>
                  <div className={s.dzIcon}><Upload size={28} /></div>
                  <p className={s.dzTitle}>{isDragActive ? 'Drop it here!' : 'Drop your file here'}</p>
                  <p className={s.dzSub}>or click to browse</p>
                  <div className={s.tagRow}>
                    {['CSV', 'Excel (.xlsx)', 'SQL Dump'].map(t => <span key={t} className={s.tag}>{t}</span>)}
                  </div>
                </>
              )}
            </div>

            <div className={s.sizeNote}>Max file size: 100 MB</div>
          </div>

          <div className={s.card}>
            <h3 className={s.cardTitle}>What happens after upload</h3>
            {[
              ['Data ingestion', 'Your file is parsed and stored securely in our PostgreSQL warehouse'],
              ['Schema extraction', 'All tables, columns, and data types are mapped automatically'],
              ['Gemini AI analysis', 'AI identifies key metrics, dimensions, and analytics opportunities'],
              ['AI data selection', 'Only the most meaningful rows and columns are forwarded'],
              ['Superset charts', 'Charts are generated exclusively from AI-selected fields'],
              ['RAG chatbot', 'Embeddings built so you can query your data in natural language'],
            ].map(([title, desc], i) => (
              <div key={i} className={s.infoStep}>
                <div className={s.infoNum}>{i + 1}</div>
                <div>
                  <div className={s.infoTitle}>{title}</div>
                  <div className={s.infoDesc}>{desc}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Connect mode ─────────────────────────────────────────────────── */}
      {mode === 'connect' && (
        <div className={s.grid}>
          <div>
            <p className={s.dbLabel}>Select database type</p>
            <div className={s.dbGrid}>
              {DB_TYPES.map(db => (
                <button
                  key={db.id}
                  className={`${s.dbCard} ${selectedDB?.id === db.id ? s.dbCardSelected : ''}`}
                  onClick={() => handleSelectDB(db)}
                >
                  <span className={s.dbEmoji}>{db.emoji}</span>
                  <span className={s.dbName}>{db.label}</span>
                  {selectedDB?.id === db.id && <CheckCircle size={14} className={s.dbCheck} />}
                </button>
              ))}
            </div>

            {selectedDB && (
              <div className={s.connForm}>
                <div className={s.formRow}>
                  <Field label="Connection alias" value={connForm.alias} onChange={set('alias')} placeholder="My Production DB" />
                </div>

                {selectedDB.id !== 'firebase' ? (
                  <>
                    <div className={s.formRow2}>
                      <Field label="Host" value={connForm.host} onChange={set('host')} placeholder={selectedDB.id === 'snowflake' ? 'account.snowflakecomputing.com' : 'db.example.com'} />
                      {selectedDB.id !== 'snowflake' && (
                        <Field label="Port" value={connForm.port} onChange={set('port')} placeholder={selectedDB.defaultPort?.toString()} />
                      )}
                    </div>
                    <div className={s.formRow2}>
                      <Field label="Database" value={connForm.database} onChange={set('database')} placeholder="my_database" />
                      {selectedDB.id === 'snowflake' && (
                        <Field label="Warehouse" value={connForm.warehouse} onChange={set('warehouse')} placeholder="COMPUTE_WH" />
                      )}
                    </div>
                    <div className={s.formRow2}>
                      <Field label="Username" value={connForm.username} onChange={set('username')} placeholder={selectedDB.id === 'postgresql' ? 'postgres' : 'root'} />
                      <Field label="Password" value={connForm.password} onChange={set('password')} placeholder="••••••••" type="password" />
                    </div>
                    {selectedDB.id === 'snowflake' && (
                      <Field label="Schema (optional)" value={connForm.schema} onChange={set('schema')} placeholder="PUBLIC" />
                    )}
                    {selectedDB.id === 'postgresql' && (
                      <Field label="Account name (Snowflake only)" value={connForm.account} onChange={set('account')} placeholder="Not required for PostgreSQL" disabled />
                    )}
                  </>
                ) : (
                  <>
                    <div className={s.formRow}>
                      <label className={s.fieldLabel}>Service account JSON</label>
                      <div className={s.fileInputWrapper}>
                        <input
                          type="file"
                          accept=".json"
                          id="firebase-json-upload"
                          style={{ display: 'none' }}
                          onChange={handleFirebaseUpload}
                        />
                        <label htmlFor="firebase-json-upload" className={s.fileInputBtn}>
                          <Upload size={14} /> {connForm.service_account_json ? 'Replace JSON' : 'Upload JSON'}
                        </label>
                        {connForm.service_account_json && (
                          <span className={s.fileName}>JSON Loaded ✅</span>
                        )}
                      </div>
                    </div>
                    <Field label="Firebase project ID" value={connForm.project_id} onChange={set('project_id')} placeholder="my-project-12345" />
                  </>
                )}

                {/* Test result */}
                {testResult === 'ok' && (
                  <div className={s.testOk}><CheckCircle size={14} /> Connection successful</div>
                )}
                {testResult === 'error' && (
                  <div className={s.testErr}><AlertCircle size={14} /> Connection failed — check your credentials</div>
                )}

                <div className={s.btnRow}>
                  <button className={s.testBtn} onClick={handleTest} disabled={testing}>
                    {testing ? <Loader size={13} className="spin" /> : null}
                    {testing ? 'Testing…' : 'Test connection'}
                  </button>
                  <button className={s.connectBtn} onClick={handleConnect} disabled={connecting}>
                    {connecting ? <Loader size={13} className="spin" /> : <ChevronRight size={13} />}
                    {connecting ? 'Connecting…' : 'Connect & analyze'}
                  </button>
                </div>
              </div>
            )}
          </div>

          <div className={s.card}>
            <h3 className={s.cardTitle}>Supported databases</h3>
            {DB_TYPES.map(db => (
              <div key={db.id} className={s.dbInfo}>
                <span className={s.dbEmoji}>{db.emoji}</span>
                <div>
                  <div className={s.infoTitle}>{db.label}</div>
                  <div className={s.infoDesc}>{dbDescription(db.id)}</div>
                </div>
              </div>
            ))}
            <div className={s.aiNote}>
              After connecting, Gemini AI automatically reads your schema, identifies meaningful tables and columns, and generates a dashboard — no manual configuration required.
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function Field({ label, value, onChange, placeholder, type = 'text', disabled = false }) {
  return (
    <div style={{ display:'flex', flexDirection:'column', gap:4 }}>
      <label style={{ fontSize:11, fontWeight:500, color:'var(--gray-600)' }}>{label}</label>
      <input
        type={type}
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        disabled={disabled}
        style={{
          padding:'8px 10px', borderRadius:'var(--radius-md)',
          border:'1px solid var(--border)', fontSize:13,
          color:'var(--gray-900)', background: disabled ? 'var(--surface-2)' : 'var(--white)',
          outline:'none', fontFamily:'inherit',
        }}
        onFocus={e => e.target.style.borderColor = 'var(--navy-400)'}
        onBlur={e => e.target.style.borderColor = 'var(--border)'}
      />
    </div>
  )
}

function dbDescription(id) {
  return {
    postgresql: 'Direct host connection — reads all public schema tables',
    mysql: 'TCP connection — reads information_schema',
    snowflake: 'Warehouse integration — reads current schema',
    firebase: 'Firestore — samples documents to infer field types',
  }[id]
}
