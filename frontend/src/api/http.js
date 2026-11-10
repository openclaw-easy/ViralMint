import axios from "axios"

const http = axios.create({
  baseURL: window.location.origin,  // works for both dev (localhost:16888) and packaged Electron app
  timeout: 30000,
})

http.interceptors.response.use(
  res => res,
  err => {
    // A cancel is ours (a request that a newer one replaced), not an error. Everything else still logs.
    if (!axios.isCancel(err)) console.error("API error:", err.response?.data || err.message)
    return Promise.reject(err)
  }
)

export default http
