import { Typography } from "@mui/material"
import FastRewindOutlinedIcon from "@mui/icons-material/FastRewindOutlined"
import ToolRunner from "../../components/tools/ToolRunner"
import useDocumentTitle from "../../hooks/useDocumentTitle"
import { MEDIA_ACCEPT } from "../../components/tools/mediaAccept"

export default function ToolRemoveSilence() {
  useDocumentTitle("Silence Remover")
  return (
    <ToolRunner
      title="Silence Remover"
      description="Auto-cut pauses and dead air"
      icon={<FastRewindOutlinedIcon fontSize="large" />}
      endpoint="/api/tools/remove-silence"
      acceptExts={MEDIA_ACCEPT}
    >
      <Typography variant="body2" sx={{ color: "text.secondary" }}>
        Detects silent gaps (quieter than -35 dB for over 0.6 s) and cuts them
        out, then re-encodes the kept ranges into one continuous file. Takes a
        video or a bare audio file — a podcast comes back as mp3. Works best on
        talking-head content with clean speech; a source with no detectable
        silence is returned unchanged.
      </Typography>
    </ToolRunner>
  )
}
