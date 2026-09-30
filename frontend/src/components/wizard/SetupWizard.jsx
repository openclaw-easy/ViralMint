import { useState, useEffect } from "react"
import {
  Dialog, DialogTitle, DialogContent, DialogActions,
  IconButton, Typography, Box, Button, LinearProgress, Stack,
} from "@mui/material"
import CloseIcon from "@mui/icons-material/Close"
import CheckCircleIcon from "@mui/icons-material/CheckCircle"
import { ws } from "../../api/websocket"
import useAppStore from "../../store/appStore"
import WizardStep from "./WizardStep"

export default function SetupWizard() {
  const activeWizard = useAppStore((s) => s.activeWizard)
  const setActiveWizard = useAppStore((s) => s.setActiveWizard)
  const [currentStepIdx, setCurrentStepIdx] = useState(0)
  const [stepStatus, setStepStatus] = useState(null)

  useEffect(() => {
    setCurrentStepIdx(0)
    setStepStatus(null)
  }, [activeWizard?.id])

  useEffect(() => {
    const unsub = ws.on("wizard_step_result", (msg) => {
      setStepStatus(msg)
      if (msg.status === "success") {
        setTimeout(() => {
          setCurrentStepIdx((i) => i + 1)
          setStepStatus(null)
        }, 800)
      }
    })
    return unsub
  }, [])

  if (!activeWizard) return null

  const steps = activeWizard.steps || []
  const currentStep = steps[currentStepIdx]
  const isComplete = currentStepIdx >= steps.length
  const progress = steps.length > 0 ? (currentStepIdx / steps.length) * 100 : 0

  const handleStepComplete = (stepId, value, field) => {
    if (field) {
      ws.send({ type: "wizard_step_complete", wizard_id: activeWizard.id, step: stepId, value, field })
    } else {
      setCurrentStepIdx((i) => i + 1)
    }
  }

  const handleClose = () => {
    ws.send({ type: "wizard_cancel", wizard_id: activeWizard.id })
    setActiveWizard(null)
  }

  return (
    <Dialog open maxWidth="sm" fullWidth onClose={handleClose} slotProps={{ paper: { sx: { bgcolor: "background.paper" } } }}>
      <DialogTitle sx={{ display: "flex", alignItems: "center", justifyContent: "space-between", pb: 1 }}>
        <Box>
          <Typography variant="h6">{activeWizard.title}</Typography>
          <Typography variant="body2" sx={{ color: "text.secondary" }}>{activeWizard.description}</Typography>
        </Box>
        <IconButton onClick={handleClose} size="small" sx={{ color: "text.secondary" }}>
          <CloseIcon />
        </IconButton>
      </DialogTitle>

      <LinearProgress variant="determinate" value={isComplete ? 100 : progress} color="primary" sx={{ mx: 3 }} />

      <DialogContent sx={{ pt: 2 }}>
        {isComplete ? (
          <Stack alignItems="center" spacing={2} sx={{ py: 4 }}>
            <CheckCircleIcon sx={{ fontSize: 48, color: "primary.main" }} />
            <Typography variant="h6" sx={{ color: "primary.main" }}>Setup complete!</Typography>
            <Button variant="contained" onClick={() => setActiveWizard(null)}>Done</Button>
          </Stack>
        ) : (
          <>
            <Typography variant="caption" sx={{ color: "text.secondary" }}>
              Step {currentStepIdx + 1} of {steps.length}
            </Typography>
            <WizardStep step={currentStep} onComplete={handleStepComplete} onCancel={handleClose} />
            {stepStatus && (
              <Typography sx={{ mt: 1, color: stepStatus.status === "success" ? "success.main" : "error.main", fontSize: "0.85rem" }}>
                {stepStatus.message}
              </Typography>
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
