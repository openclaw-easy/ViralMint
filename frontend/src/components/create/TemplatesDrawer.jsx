// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2025-2026 ViralMint Contributors
//
// Right-anchored Templates drawer for the Smart Video studio. Keeps the
// "pick a starting point" gallery out of the main studio's vertical space —
// it slides out on demand and closes once a template is applied.
import { Box, Typography, IconButton, Drawer, Divider } from "@mui/material"
import CloseIcon from "@mui/icons-material/Close"
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome"
import TemplateGallery from "./TemplateGallery"

export default function TemplatesDrawer({ open, onClose, mode = "stock", onApply }) {
  return (
    <Drawer
      anchor="right"
      open={open}
      onClose={onClose}
      PaperProps={{ sx: { width: { xs: "100%", sm: 460 }, maxWidth: "100%" } }}
    >
      <Box sx={{ px: 2, py: 1.5, display: "flex", alignItems: "center", justifyContent: "space-between", flexShrink: 0 }}>
        <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
          <AutoAwesomeIcon sx={{ fontSize: 20, color: "primary.main" }} />
          <Box>
            <Typography variant="subtitle1" sx={{ fontWeight: 700, lineHeight: 1.1 }}>Templates</Typography>
            <Typography variant="caption" sx={{ color: "text.secondary" }}>Pick a starting point</Typography>
          </Box>
        </Box>
        <IconButton onClick={onClose} size="small"><CloseIcon /></IconButton>
      </Box>
      <Divider />
      <Box sx={{ flex: 1, overflowY: "auto" }}>
        <TemplateGallery
          mode={mode}
          variant="drawer"
          onApply={(defaults) => { onApply(defaults); onClose() }}
        />
      </Box>
    </Drawer>
  )
}
