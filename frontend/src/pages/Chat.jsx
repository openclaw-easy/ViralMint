import { useEffect, useRef, useState, useCallback } from "react"
import {
  Box, Typography, Button, Stack, CircularProgress, LinearProgress,
  List, ListItemButton, ListItemText, IconButton,
  Divider, Dialog, DialogTitle, DialogContent, DialogActions,
} from "@mui/material"
import AddIcon from "@mui/icons-material/Add"
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline"
import HistoryIcon from "@mui/icons-material/HistoryOutlined"
import ChevronRightIcon from "@mui/icons-material/ChevronRight"
import RadarIcon from "@mui/icons-material/RadarOutlined"
import DownloadIcon from "@mui/icons-material/DownloadOutlined"
import MovieCreationIcon from "@mui/icons-material/MovieCreationOutlined"
import UploadIcon from "@mui/icons-material/UploadOutlined"
import SearchIcon from "@mui/icons-material/TravelExploreOutlined"
import AnalyticsIcon from "@mui/icons-material/InsightsOutlined"
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesomeOutlined"
import SensorsIcon from "@mui/icons-material/SensorsOutlined"
import NewspaperIcon from "@mui/icons-material/NewspaperOutlined"
import CheckCircleOutlineIcon from "@mui/icons-material/CheckCircleOutline"
import ErrorOutlineIcon from "@mui/icons-material/ErrorOutline"
import { ws } from "../api/websocket"
import http from "../api/http"
import useAppStore from "../store/appStore"
import ChatMessage from "../components/chat/ChatMessage"
import QuickReplyChips from "../components/chat/QuickReplyChips"
import ChatInput from "../components/chat/ChatInput"
import SetupWizard from "../components/wizard/SetupWizard"
import ScoutResultsCard from "../components/chat/ScoutResultsCard"
import JobProgressCard from "../components/chat/JobProgressCard"
import VideoPreviewCard from "../components/chat/VideoPreviewCard"
import InsightsCard from "../components/chat/InsightsCard"
import ChannelSummaryCard from "../components/chat/ChannelSummaryCard"
import DownloadedListCard from "../components/chat/DownloadedListCard"
import ContentCalendarCard from "../components/chat/ContentCalendarCard"
import NewsResultsCard from "../components/chat/NewsResultsCard"

function timeAgo(dateString) {
  if (!dateString) return ""
  const diff = Date.now() - new Date(dateString).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return "just now"
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  if (days === 1) return "Yesterday"
  if (days < 7) return `${days}d ago`
  return new Date(dateString).toLocaleDateString(undefined, { month: "short", day: "numeric" })
}

function RichMessage({ msg }) {
  const wrapper = { maxWidth: 900, mx: "auto", width: "100%", px: 1, py: 1 }
  switch (msg.type) {
    case "scout_results":
      return <Box sx={wrapper}><ScoutResultsCard results={msg.data.results} platform={msg.data.platform} jobId={msg.data.jobId} /></Box>
    case "job_progress":
      return <Box sx={wrapper}><JobProgressCard jobId={msg.data.jobId} jobType={msg.data.jobType} message={msg.data.message} /></Box>
    case "video_preview":
      return <Box sx={wrapper}><VideoPreviewCard video={msg.data.video} /></Box>
    case "insights":
      return <Box sx={wrapper}><InsightsCard videos={msg.data.videos} /></Box>
    case "channel_summary":
      return <Box sx={wrapper}><ChannelSummaryCard summary={msg.data.summary} /></Box>
    case "downloaded_list":
      return <Box sx={wrapper}><DownloadedListCard videos={msg.data.videos} /></Box>
    case "content_calendar":
      return <Box sx={wrapper}><ContentCalendarCard calendar={msg.data.calendar} /></Box>
    case "news_results":
      return <Box sx={wrapper}><NewsResultsCard results={msg.data.results} query={msg.data.query} /></Box>
    default:
      return <ChatMessage role="system" content={JSON.stringify(msg.data)} />
  }
}

const JOB_ICONS = {
  scout: <RadarIcon sx={{ fontSize: 14 }} />,
  download: <DownloadIcon sx={{ fontSize: 14 }} />,
  generate: <MovieCreationIcon sx={{ fontSize: 14 }} />,
  upload: <UploadIcon sx={{ fontSize: 14 }} />,
}

const JOB_LABELS = {
  scout: "Scouting", download: "Downloading", generate: "Generating", upload: "Uploading",
}

function ActiveJobsPanel() {
  const activeJobs = useAppStore((s) => s.activeJobs)
  const removeJob = useAppStore((s) => s.removeJob)
  const jobList = Object.values(activeJobs)

  if (jobList.length === 0) return null

  return (
    <Box sx={{ px: 1.5, pb: 1 }}>
      <Typography variant="caption" sx={{ color: "text.secondary", fontWeight: 600, fontSize: "0.7rem", textTransform: "uppercase", letterSpacing: 0.5 }}>
        Active Jobs
      </Typography>
      <Stack spacing={0.5} sx={{ mt: 0.5 }}>
        {jobList.map((job) => {
          const isRunning = job.status === "running"
          const isSuccess = job.status === "success"
          const isFailed = job.status === "failed"
          const label = JOB_LABELS[job.jobType] || job.jobType
          return (
            <Box key={job.jobId} sx={{
              bgcolor: "action.hover", borderRadius: 1.5, px: 1, py: 0.75,
              border: 1, borderColor: isSuccess ? "success.main" : isFailed ? "error.main" : "divider",
              cursor: isFailed || isSuccess ? "pointer" : "default",
              "&:hover": (isFailed || isSuccess) ? { opacity: 0.7 } : {},
            }}
              onClick={() => { if (isFailed || isSuccess) removeJob(job.jobId) }}
              title={isFailed || isSuccess ? "Click to dismiss" : ""}
            >
              <Stack direction="row" spacing={0.5} alignItems="center" sx={{ mb: isRunning ? 0.5 : 0 }}>
                {isSuccess ? <CheckCircleOutlineIcon sx={{ fontSize: 14, color: "success.main" }} /> :
                  isFailed ? <ErrorOutlineIcon sx={{ fontSize: 14, color: "error.main" }} /> :
                  JOB_ICONS[job.jobType] || JOB_ICONS.scout}
                <Typography variant="caption" sx={{ fontWeight: 500, fontSize: "0.72rem", color: "text.primary", flex: 1 }} noWrap>
                  {isSuccess ? `${label} done` : isFailed ? `${label} failed` : `${label}...`}
                </Typography>
                {isRunning && job.percent > 0 && (
                  <Typography variant="caption" sx={{ fontSize: "0.65rem", color: "text.secondary" }}>
                    {Math.round(job.percent)}%
                  </Typography>
                )}
              </Stack>
              {isRunning && (
                <LinearProgress
                  variant={job.percent > 0 ? "determinate" : "indeterminate"}
                  value={job.percent}
                  sx={{ borderRadius: 1, height: 3 }}
                />
              )}
              {job.step && !isSuccess && (
                <Typography variant="caption" sx={{ color: "text.secondary", fontSize: "0.65rem", mt: 0.25, display: "block" }} noWrap>
                  {job.step}
                </Typography>
              )}
            </Box>
          )
        })}
      </Stack>
    </Box>
  )
}

const SIDEBAR_WIDTH = 260

const STARTER_SUGGESTIONS = [
  {
    icon: <SearchIcon sx={{ fontSize: 28, color: "primary.main" }} />,
    title: "Scout trending videos",
    description: "Find viral content across YouTube, TikTok, and Douyin",
    message: "Scout trending videos on YouTube",
  },
  {
    icon: <SensorsIcon sx={{ fontSize: 28, color: "primary.main" }} />,
    title: "Analyze a channel",
    description: "Paste a YouTube or TikTok channel URL for a full breakdown",
    message: "Analyze a YouTube channel for me",
  },
  {
    icon: <AnalyticsIcon sx={{ fontSize: 28, color: "primary.main" }} />,
    title: "Download & analyze",
    description: "Transcribe competitors and extract viral insights",
    message: "Download and analyze a video",
  },
  {
    icon: <AutoAwesomeIcon sx={{ fontSize: 28, color: "primary.main" }} />,
    title: "Generate AI video",
    description: "Create original videos with AI voice and visuals",
    message: "Generate an original video",
  },
  {
    icon: <NewspaperIcon sx={{ fontSize: 28, color: "primary.main" }} />,
    title: "Scout trending news",
    description: "Find hot articles and generate commentary videos",
    message: "Scout trending news",
  },
]

export default function Chat() {
  const sessions = useAppStore((s) => s.sessions)
  const activeSessionId = useAppStore((s) => s.activeSessionId)
  const activeJobs = useAppStore((s) => s.activeJobs)
  const messages = useAppStore((s) => s.messages)
  const isStreaming = useAppStore((s) => s.isStreaming)
  const streamingText = useAppStore((s) => s.streamingText)
  const suggestions = useAppStore((s) => s.suggestions)
  const addMessage = useAppStore((s) => s.addMessage)
  const setStreaming = useAppStore((s) => s.setStreaming)
  const clearStreamingText = useAppStore((s) => s.clearStreamingText)
  const setSuggestions = useAppStore((s) => s.setSuggestions)
  const setMessages = useAppStore((s) => s.setMessages)
  const setActiveSessionId = useAppStore((s) => s.setActiveSessionId)
  const setSessions = useAppStore((s) => s.setSessions)
  const removeSession = useAppStore((s) => s.removeSession)
  const showSnackbar = useAppStore((s) => s.showSnackbar)
  const bottomRef = useRef(null)

  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [confirmDialog, setConfirmDialog] = useState({ open: false, title: "", message: "", onConfirm: null })
  const openConfirm = (title, message, onConfirm) => setConfirmDialog({ open: true, title, message, onConfirm })
  const closeConfirm = () => setConfirmDialog(prev => ({ ...prev, open: false }))

  // Load sessions on mount + restore active session from sessionStorage
  useEffect(() => {
    http.get("/api/chat/sessions").then(res => {
      setSessions(res.data)
    }).catch((err) => {
      console.error("Failed to load chat sessions:", err)
      showSnackbar("Could not load chat history", "warning")
    })

    const savedSessionId = sessionStorage.getItem("vm_active_session")
    if (savedSessionId && !activeSessionId && messages.length === 0) {
      // Restore the session that was active before refresh
      setActiveSessionId(savedSessionId)
      setSuggestions([])
      ws.send({ type: "set_session", session_id: savedSessionId })
      http.get(`/api/chat/sessions/${savedSessionId}/messages`).then(res => {
        const loaded = res.data.map(m => {
          if (m.role === "rich" || m.msg_type) {
            return { role: "rich", type: m.msg_type, data: m.data_json ? JSON.parse(m.data_json) : {} }
          }
          return { role: m.role, content: m.content }
        })
        setMessages(loaded)
        setSuggestions([])  // clear again after messages load, in case smart_suggestions arrived
      }).catch((err) => {
        console.error("Failed to restore session messages:", err)
        sessionStorage.removeItem("vm_active_session")
        setActiveSessionId(null)
        setSuggestions(STARTER_SUGGESTIONS)
      })
    } else if (!savedSessionId && suggestions.length === 0 && messages.length === 0) {
      setSuggestions(STARTER_SUGGESTIONS)
    }
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [messages, streamingText])

  // Auto-open sidebar when a running job appears
  const runningJobCount = Object.values(activeJobs).filter(j => j.status === "running").length
  useEffect(() => {
    if (runningJobCount > 0) setSidebarOpen(true)
  }, [runningJobCount])

  const switchSession = useCallback(async (sessionId) => {
    if (sessionId === activeSessionId) return
    setActiveSessionId(sessionId)
    setMessages([])
    setSuggestions([])
    ws.send({ type: "set_session", session_id: sessionId })

    try {
      const res = await http.get(`/api/chat/sessions/${sessionId}/messages`)
      const loaded = res.data.map(m => {
        if (m.role === "rich" || m.msg_type) {
          return { role: "rich", type: m.msg_type, data: m.data_json ? JSON.parse(m.data_json) : {} }
        }
        return { role: m.role, content: m.content }
      })
      setMessages(loaded)
    } catch (err) {
      console.warn("[Chat] Failed to restore session messages:", err)
    }
  }, [activeSessionId])

  const handleNewChat = async () => {
    setActiveSessionId(null)
    setMessages([])
    setSuggestions(STARTER_SUGGESTIONS)
    ws.send({ type: "set_session", session_id: null })
  }

  const handleDeleteSession = (sessionId) => {
    openConfirm("Delete chat?", "This conversation will be permanently removed.", async () => {
      try {
        await http.delete(`/api/chat/sessions/${sessionId}`)
        removeSession(sessionId)
        if (activeSessionId === sessionId) {
          setActiveSessionId(null)
          setMessages([])
          setSuggestions(STARTER_SUGGESTIONS)
        }
        showSnackbar("Chat deleted", "info")
      } catch {
        showSnackbar("Failed to delete chat", "error")
      }
    })
  }

  const handleSend = (text) => {
    addMessage({ role: "user", content: text })
    setSuggestions([])
    setStreaming(true)
    clearStreamingText()
    if (activeSessionId) {
      ws.send({ type: "set_session", session_id: activeSessionId })
    }
    ws.send({ type: "chat_message", content: text })
  }

  return (
    <Box sx={{ display: "flex", height: "100%", position: "relative" }}>
      {/* Chat area */}
      <Box sx={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        {/* Top bar with history toggle */}
        <Box sx={{
          display: "flex", justifyContent: "space-between", alignItems: "center",
          px: 2, py: 1, flexShrink: 0,
          borderBottom: 1, borderColor: "divider",
          background: (t) => t.palette.mode === "dark"
            ? "linear-gradient(135deg, rgba(201,100,66,0.08) 0%, rgba(30,28,26,1) 100%)"
            : "linear-gradient(135deg, rgba(201,100,66,0.05) 0%, rgba(255,255,255,1) 100%)",
        }}>
          <Button
            size="small"
            variant="contained"
            startIcon={<AddIcon />}
            onClick={handleNewChat}
          >
            New chat
          </Button>
          <IconButton
            onClick={() => setSidebarOpen(o => !o)}
            size="small"
            sx={{ color: "text.secondary" }}
          >
            <HistoryIcon fontSize="small" />
          </IconButton>
        </Box>

        {/* Messages area */}
        <Box sx={{ flex: 1, overflow: "auto", py: 2 }}>
          {messages.length === 0 && !isStreaming && (
            <Box sx={{
              display: "flex", flexDirection: "column", alignItems: "center",
              justifyContent: "center", flex: 1, minHeight: "60vh", px: 3,
            }}>
              {/* Gradient glow background */}
              <Box sx={{
                position: "absolute", top: "15%", left: "50%", transform: "translateX(-50%)",
                width: 400, height: 400, borderRadius: "50%", filter: "blur(100px)", opacity: 0.08,
                background: "linear-gradient(135deg, #c96442, #e88a5a, #c96442)",
                animation: "pulse 8s ease-in-out infinite",
                "@keyframes pulse": {
                  "0%, 100%": { opacity: 0.06, transform: "translateX(-50%) scale(1)" },
                  "50%": { opacity: 0.12, transform: "translateX(-50%) scale(1.1)" },
                },
                pointerEvents: "none",
              }} />

              <Typography
                variant="h4"
                sx={{
                  mb: 1, fontWeight: 700, fontSize: "2rem", letterSpacing: -0.5,
                  background: "linear-gradient(135deg, #c96442, #e88a5a)",
                  WebkitBackgroundClip: "text",
                  WebkitTextFillColor: "transparent",
                }}
              >
                ViralMint
              </Typography>
              <Typography
                variant="body1"
                sx={{ color: "text.secondary", mb: 5, maxWidth: 440, mx: "auto", lineHeight: 1.6, textAlign: "center" }}
              >
                Your AI content strategy assistant. Scout trending videos, generate originals, and publish everywhere.
              </Typography>

              {suggestions.length > 0 && (
                <Box sx={{
                  display: "grid",
                  gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr", md: "1fr 1fr 1fr" },
                  gap: 1.5,
                  maxWidth: 680,
                  width: "100%",
                }}>
                  {suggestions.map((s, i) => (
                    <Box
                      key={i}
                      onClick={() => handleSend(typeof s === "string" ? s : s.message)}
                      sx={{
                        p: 2,
                        borderRadius: 3,
                        border: 1,
                        borderColor: "divider",
                        bgcolor: "background.paper",
                        cursor: "pointer",
                        transition: "all 0.2s ease",
                        boxShadow: (theme) => theme.customShadows?.sm,
                        "&:hover": {
                          borderColor: "primary.main",
                          transform: "translateY(-2px)",
                          boxShadow: (theme) => `${theme.customShadows?.md}, ${theme.customShadows?.glow}`,
                        },
                      }}
                    >
                      <Box sx={{ mb: 1 }}>
                        {typeof s === "string" ? null : s.icon}
                      </Box>
                      <Typography sx={{ fontWeight: 600, fontSize: "0.9rem", color: "text.primary", mb: 0.25 }}>
                        {typeof s === "string" ? s : s.title}
                      </Typography>
                      {typeof s !== "string" && (
                        <Typography sx={{ fontSize: "0.8rem", color: "text.secondary", lineHeight: 1.4 }}>
                          {s.description}
                        </Typography>
                      )}
                    </Box>
                  ))}
                </Box>
              )}
            </Box>
          )}

          {messages.map((msg, i) => {
            if (msg.role === "rich") return <RichMessage key={i} msg={msg} />
            return (
              <Box key={i}>
                <ChatMessage role={msg.role} content={msg.content} />
                {msg.quickReplies?.length > 0 && (
                  <Box sx={{ maxWidth: 900, mx: "auto", width: "100%", px: 1, pl: { xs: 1, sm: 6.5 } }}>
                    <QuickReplyChips suggestions={msg.quickReplies} onSelect={handleSend} />
                  </Box>
                )}
              </Box>
            )
          })}

          {isStreaming && streamingText && (
            <ChatMessage role="assistant" content={streamingText} />
          )}
          {isStreaming && !streamingText && (
            <Box sx={{ display: "flex", alignItems: "center", gap: 1, py: 2, px: 1, maxWidth: 900, mx: "auto" }}>
              <CircularProgress size={16} sx={{ color: "text.secondary" }} />
              <Typography variant="body2" sx={{ color: "text.secondary" }}>Thinking...</Typography>
            </Box>
          )}

          <div ref={bottomRef} />
        </Box>

        <ChatInput onSend={handleSend} disabled={isStreaming} />
        <SetupWizard />
      </Box>

      {/* History sidebar — right side, collapsible */}
      <Box
        sx={{
          width: sidebarOpen ? SIDEBAR_WIDTH : 0,
          flexShrink: 0,
          overflow: "hidden",
          transition: "width 0.25s ease",
          borderLeft: sidebarOpen ? 1 : 0,
          borderColor: "divider",
          bgcolor: "background.paper",
        }}
      >
        <Box sx={{ width: SIDEBAR_WIDTH, height: "100%", display: "flex", flexDirection: "column" }}>
          {/* Header */}
          <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ px: 1.5, py: 1.25 }}>
            <Typography variant="body2" sx={{ fontWeight: 600, color: "text.primary", fontSize: "0.85rem" }}>
              History
            </Typography>
            <IconButton size="small" onClick={() => setSidebarOpen(false)} sx={{ color: "text.secondary" }}>
              <ChevronRightIcon fontSize="small" />
            </IconButton>
          </Stack>

          <Divider />

          {/* Active jobs */}
          <ActiveJobsPanel />

          <Divider />

          {/* Session list */}
          <Box sx={{ flex: 1, overflow: "auto", py: 0.5, px: 1 }}>
            <List disablePadding>
              {sessions.map((sess) => (
                <ListItemButton
                  key={sess.id}
                  selected={sess.id === activeSessionId}
                  onClick={() => switchSession(sess.id)}
                  sx={{
                    borderRadius: 1.5,
                    mb: 0.25,
                    py: 0.5,
                    px: 1,
                    minHeight: 36,
                    pr: 4,
                    position: "relative",
                    "&:hover .session-menu-btn": { opacity: 1 },
                  }}
                >
                  <ListItemText
                    primary={sess.title}
                    secondary={timeAgo(sess.updated_at || sess.created_at)}
                    slotProps={{
                      primary: {
                        noWrap: true,
                        fontSize: "0.825rem",
                        fontWeight: sess.id === activeSessionId ? 600 : 400,
                        color: "text.primary",
                      },
                      secondary: {
                        noWrap: true,
                        fontSize: "0.7rem",
                      },
                    }}
                  />
                  <IconButton
                    className="session-menu-btn"
                    size="small"
                    onClick={(e) => { e.stopPropagation(); handleDeleteSession(sess.id) }}
                    sx={{
                      position: "absolute",
                      right: 2,
                      opacity: 0,
                      transition: "opacity 0.15s",
                      width: 24,
                      height: 24,
                      color: "text.secondary",
                      "&:hover": { color: "error.main" },
                    }}
                  >
                    <DeleteOutlineIcon sx={{ fontSize: 15 }} />
                  </IconButton>
                </ListItemButton>
              ))}
            </List>

            {sessions.length === 0 && (
              <Typography variant="caption" sx={{ color: "text.secondary", display: "block", textAlign: "center", mt: 3 }}>
                No conversations yet
              </Typography>
            )}
          </Box>
        </Box>
      </Box>

      {/* Delete confirmation dialog */}
      <Dialog open={confirmDialog.open} onClose={closeConfirm} maxWidth="xs" fullWidth>
        <DialogTitle>{confirmDialog.title}</DialogTitle>
        <DialogContent><Typography>{confirmDialog.message}</Typography></DialogContent>
        <DialogActions>
          <Button onClick={closeConfirm}>Cancel</Button>
          <Button color="error" variant="contained" onClick={() => { confirmDialog.onConfirm?.(); closeConfirm() }}>Delete</Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
}
