import { useState, useEffect } from "react"
import {
  Box, Stack, Typography, Alert, TextField, Button, FormControl, InputLabel,
  Select, MenuItem, IconButton, InputAdornment, Chip, Dialog, DialogTitle,
  DialogContent, DialogActions,
} from "@mui/material"
import VisibilityIcon from "@mui/icons-material/VisibilityOutlined"
import VisibilityOffIcon from "@mui/icons-material/VisibilityOffOutlined"
import SaveIcon from "@mui/icons-material/SaveOutlined"
import EditIcon from "@mui/icons-material/EditOutlined"
import DeleteIcon from "@mui/icons-material/DeleteOutlineOutlined"
import CheckCircleIcon from "@mui/icons-material/CheckCircleOutline"
import http from "../../api/http"
import useAppStore from "../../store/appStore"

const PROVIDER_LABEL = {
  anthropic: "Anthropic (Claude)",
  openai: "OpenAI (GPT)",
  openrouter: "OpenRouter (Claude / GPT / Gemini / Llama)",
  openai_compatible: "OpenAI-compatible (Ollama, LM Studio, vLLM, …)",
}

// Any server that speaks OpenAI's /chat/completions, at a URL the user gives.
const COMPATIBLE = "openai_compatible"

const KEY_PLACEHOLDER = {
  anthropic: "sk-ant-...",
  openai: "sk-...",
  openrouter: "sk-or-...",
  openai_compatible: "Optional — local servers usually need none",
}

const KEY_HINT = {
  anthropic: "Get yours at console.anthropic.com → API Keys",
  openai: "Get yours at platform.openai.com → API Keys",
  openrouter: "One key, every model — get yours at openrouter.ai/keys",
  openai_compatible: "Only if your server asks for one (DeepSeek, Groq, a gateway…)",
}

export default function AIProviderSection({ settings, updateSettings }) {
  const showSnackbar = useAppStore((s) => s.showSnackbar)

  const [provider, setProvider] = useState(settings?.ai_provider || "openai")
  const [model, setModel] = useState(settings?.ai_model || "")
  const [baseUrl, setBaseUrl] = useState(settings?.ai_base_url || "")
  const [apiKey, setApiKey] = useState("")
  const [showKey, setShowKey] = useState(false)
  const [registry, setRegistry] = useState({})
  const [saving, setSaving] = useState(false)
  const isConfigured = (s) =>
    !!s?.ai_api_key_set || (s?.ai_provider === COMPATIBLE && !!s?.ai_base_url)
  const [editing, setEditing] = useState(!isConfigured(settings))
  const [confirmRemove, setConfirmRemove] = useState(false)

  // Sync local state when settings change (e.g. after save).
  // If no key is set (cleared elsewhere or never set), force into edit mode
  // so the form is visible. handleSave / handleRemove own toggling out.
  useEffect(() => {
    if (settings) {
      setProvider(settings.ai_provider || "openai")
      setModel(settings.ai_model || "")
      setBaseUrl(settings.ai_base_url || "")
      if (!isConfigured(settings)) setEditing(true)
    }
  }, [settings?.ai_provider, settings?.ai_model, settings?.ai_api_key_set, settings?.ai_base_url])

  // Fetch model registry once
  useEffect(() => {
    http.get("/api/config/model_registry")
      .then(({ data }) => setRegistry(data?.value || {}))
      .catch(() => {})
  }, [])

  const availableModels = registry[provider]?.models || []
  const defaultModel = registry[provider]?.default_model || ""
  const effectiveModel = model || defaultModel
  const compatible = provider === COMPATIBLE

  const handleSave = async () => {
    const hasNewKey = apiKey.trim().length > 0
    if (compatible) {
      if (!baseUrl.trim() || !model.trim()) {
        showSnackbar("Enter the server's base URL and a model name", "warning")
        return
      }
    } else if (!hasNewKey && !settings?.ai_api_key_set) {
      showSnackbar("Paste your API key first", "warning")
      return
    }
    setSaving(true)
    try {
      // Always send the model that's actually displayed in the dropdown.
      // If the user switched providers, `model` was reset to "" and `effectiveModel`
      // resolves to the new provider's default — that's what they saw and what we save.
      const updates = {
        ai_provider: provider,
        ai_model: (compatible ? model.trim() : effectiveModel) || "",  // empty string clears stale model
      }
      if (compatible) updates.ai_base_url = baseUrl.trim()
      if (hasNewKey) {
        updates.ai_api_key = apiKey.trim()
      }
      await updateSettings(updates)
      setApiKey("")
      setEditing(false)
      showSnackbar(hasNewKey ? "API key saved" : compatible ? "Server saved" : "Provider updated", "success")
    } catch (e) {
      // updateSettings already shows a snackbar on error
    } finally {
      setSaving(false)
    }
  }

  const handleRemove = async () => {
    setConfirmRemove(false)
    setSaving(true)
    try {
      await updateSettings(
        settings?.ai_provider === COMPATIBLE
          ? { ai_api_key: "", ai_base_url: "" }
          : { ai_api_key: "" },
      )
      setApiKey("")
      setEditing(true)
      showSnackbar("Provider removed — falling back to .env (if configured)", "info")
    } catch (e) {
      // handled in updateSettings
    } finally {
      setSaving(false)
    }
  }

  const keyIsSet = !!settings?.ai_api_key_set

  // ── Connected state: key already configured, not editing ────────────────────
  if (!editing && isConfigured(settings)) {
    return (
      <Stack spacing={2}>
        <Alert
          severity="success"
          variant="outlined"
          icon={<CheckCircleIcon fontSize="small" />}
        >
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>
              {settings.ai_provider === COMPATIBLE ? "Your own AI server configured" : "Personal API key configured"}
            </Typography>
            <Chip
              size="small"
              label={PROVIDER_LABEL[settings.ai_provider] || settings.ai_provider}
              color="primary"
              variant="outlined"
              sx={{ fontWeight: 500 }}
            />
            {settings.ai_model && (
              <Chip
                size="small"
                label={`model: ${settings.ai_model}`}
                variant="outlined"
              />
            )}
            {settings.ai_provider === COMPATIBLE && settings.ai_base_url && (
              <Chip
                size="small"
                label={settings.ai_base_url}
                variant="outlined"
                sx={{ maxWidth: "100%" }}
              />
            )}
          </Stack>
          <Typography variant="caption" sx={{ color: "text.secondary", display: "block", mt: 0.75 }}>
            Stored encrypted on your machine. Sent only to the provider when chatting / generating.
          </Typography>
        </Alert>
        <Stack direction="row" spacing={1.5}>
          <Button
            variant="contained"
            startIcon={<EditIcon />}
            onClick={() => { setEditing(true); setApiKey("") }}
            disabled={saving}
          >
            Update
          </Button>
          <Button
            variant="outlined"
            color="error"
            startIcon={<DeleteIcon />}
            onClick={() => setConfirmRemove(true)}
            disabled={saving}
          >
            Remove
          </Button>
        </Stack>

        <Dialog open={confirmRemove} onClose={() => setConfirmRemove(false)} maxWidth="xs" fullWidth>
          <DialogTitle>{settings.ai_provider === COMPATIBLE ? "Remove this server?" : "Remove API key?"}</DialogTitle>
          <DialogContent>
            <Typography variant="body2">
              This deletes the {settings.ai_provider === COMPATIBLE ? "server URL and any" : "encrypted"} key
              from your local database. ViralMint will fall back to the provider configured in
              your <code>.env</code> file (if any). If neither is configured, AI features will stop
              working until you set one up again.
            </Typography>
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setConfirmRemove(false)}>Cancel</Button>
            <Button color="error" variant="contained" onClick={handleRemove}>
              {settings.ai_provider === COMPATIBLE ? "Remove server" : "Remove key"}
            </Button>
          </DialogActions>
        </Dialog>
      </Stack>
    )
  }

  // ── Editing state: form for provider/model/key ──────────────────────────────
  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        Bring your own Anthropic, OpenAI, or OpenRouter API key — or point ViralMint at any
        OpenAI-compatible server (Ollama, LM Studio, vLLM, DeepSeek, Groq…). Keys are encrypted
        (AES-256) before storage and never leave your machine except when calling the provider
        directly. OpenRouter is a unified gateway — one key, access to Claude / GPT / Gemini / Llama.
      </Typography>

      <FormControl size="small" fullWidth>
        <InputLabel>Provider</InputLabel>
        <Select
          value={provider}
          label="Provider"
          onChange={(e) => { setProvider(e.target.value); setModel("") }}
          disabled={saving}
        >
          <MenuItem value="anthropic">Anthropic (Claude)</MenuItem>
          <MenuItem value="openai">OpenAI (GPT)</MenuItem>
          <MenuItem value="openrouter">OpenRouter (Claude / GPT / Gemini / Llama)</MenuItem>
          <MenuItem value={COMPATIBLE}>{PROVIDER_LABEL[COMPATIBLE]}</MenuItem>
        </Select>
      </FormControl>

      {compatible && (
        <TextField
          label="Base URL"
          size="small"
          fullWidth
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="http://localhost:11434/v1"
          helperText="The server's OpenAI-compatible endpoint, usually ending in /v1 (Ollama: http://localhost:11434/v1, LM Studio: http://localhost:1234/v1)"
          disabled={saving}
          autoComplete="off"
        />
      )}

      {compatible ? (
        <TextField
          label="Model"
          size="small"
          fullWidth
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="e.g. qwen3:14b, llama3.3, deepseek-chat"
          helperText="Exactly as your server names it. Smaller local models may struggle with the structured (JSON) replies some features need."
          disabled={saving}
          autoComplete="off"
        />
      ) : (
      <FormControl size="small" fullWidth>
        <InputLabel>Model</InputLabel>
        <Select
          value={effectiveModel}
          label="Model"
          onChange={(e) => setModel(e.target.value)}
          disabled={saving || availableModels.length === 0}
        >
          {availableModels.map((m) => (
            <MenuItem key={m} value={m}>
              {m}
              {m === defaultModel && (
                <Typography component="span" variant="caption" sx={{ ml: 1, color: "text.secondary" }}>
                  (default)
                </Typography>
              )}
            </MenuItem>
          ))}
        </Select>
      </FormControl>
      )}

      <TextField
        label={compatible ? "API key (optional)" : "API key"}
        type={showKey ? "text" : "password"}
        size="small"
        fullWidth
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
        placeholder={keyIsSet ? "(leave empty to keep existing key)" : KEY_PLACEHOLDER[provider]}
        helperText={KEY_HINT[provider]}
        disabled={saving}
        autoComplete="off"
        InputProps={{
          endAdornment: (
            <InputAdornment position="end">
              <IconButton size="small" onClick={() => setShowKey((v) => !v)} edge="end" tabIndex={-1}>
                {showKey ? <VisibilityOffIcon fontSize="small" /> : <VisibilityIcon fontSize="small" />}
              </IconButton>
            </InputAdornment>
          ),
        }}
      />

      <Stack direction="row" spacing={1.5}>
        <Button
          variant="contained"
          startIcon={<SaveIcon />}
          onClick={handleSave}
          disabled={saving || (compatible ? !baseUrl.trim() || !model.trim() : !apiKey.trim() && !keyIsSet)}
        >
          {saving ? "Saving…" : "Save"}
        </Button>
        {isConfigured(settings) && (
          <Button
            variant="outlined"
            onClick={() => { setEditing(false); setApiKey("") }}
            disabled={saving}
          >
            Cancel
          </Button>
        )}
      </Stack>
    </Stack>
  )
}
