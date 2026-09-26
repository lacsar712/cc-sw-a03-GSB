export async function api(path, opts = {}) {
  const token = localStorage.getItem('tok') || ''
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) }
  if (token) headers.Authorization = 'Bearer ' + token
  const r = await fetch(path, { ...opts, headers })
  const t = await r.text()
  let data = {}
  try {
    data = t ? JSON.parse(t) : {}
  } catch {
    data = { detail: t }
  }
  if (!r.ok) {
    const err = new Error(data.detail || data.message || r.statusText)
    err.status = r.status
    err.data = data
    throw err
  }
  return data
}
