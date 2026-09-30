import { createTheme, alpha } from "@mui/material/styles"

// ── Shadow system ────────────────────────────────────────────────────────────
const shadows = {
  sm: "0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.02)",
  md: "0 4px 14px rgba(0,0,0,0.06), 0 2px 6px rgba(0,0,0,0.03)",
  lg: "0 12px 28px rgba(0,0,0,0.08), 0 4px 10px rgba(0,0,0,0.04)",
  xl: "0 20px 40px rgba(0,0,0,0.1), 0 8px 16px rgba(0,0,0,0.05)",
  glow: "0 0 20px rgba(201,100,66,0.12)",
  glowStrong: "0 0 28px rgba(201,100,66,0.22)",
  up: "0 -2px 8px rgba(0,0,0,0.04)",
}

const darkShadows = {
  sm: "0 1px 3px rgba(0,0,0,0.2), 0 1px 2px rgba(0,0,0,0.12)",
  md: "0 4px 14px rgba(0,0,0,0.25), 0 2px 6px rgba(0,0,0,0.15)",
  lg: "0 12px 28px rgba(0,0,0,0.35), 0 4px 10px rgba(0,0,0,0.2)",
  xl: "0 20px 40px rgba(0,0,0,0.45), 0 8px 16px rgba(0,0,0,0.25)",
  glow: "0 0 20px rgba(201,100,66,0.18)",
  glowStrong: "0 0 28px rgba(201,100,66,0.28)",
  up: "0 -2px 8px rgba(0,0,0,0.15)",
}

export default function createAppTheme(mode) {
  const isDark = mode === "dark"
  const s = isDark ? darkShadows : shadows
  const P = "#c96442"

  const theme = createTheme({
    palette: {
      mode,
      primary: { main: P, contrastText: "#fff" },
      secondary: { main: isDark ? "#9ca3af" : "#6b7280" },
      background: {
        default: isDark ? "#141210" : "#f5f0ea",
        paper: isDark ? "#1e1c1a" : "#ffffff",
        subtle: isDark ? "#1a1816" : "#faf6f2",
      },
      text: {
        primary: isDark ? "#e8e4df" : "#2d2b28",
        secondary: isDark ? "#9c9690" : "#6b6560",
      },
      success: { main: "#16a34a" },
      warning: { main: "#d97706" },
      error: { main: "#dc2626" },
      info: { main: "#2563eb" },
      divider: isDark ? "rgba(255,255,255,0.07)" : "rgba(0,0,0,0.07)",
      action: {
        hover: isDark ? "rgba(255,255,255,0.04)" : "rgba(0,0,0,0.03)",
        selected: "rgba(201,100,66,0.08)",
      },
    },
    typography: {
      fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
      h4: { fontWeight: 700, fontSize: "1.5rem", letterSpacing: "-0.02em" },
      h5: { fontWeight: 700, fontSize: "1.15rem", letterSpacing: "-0.01em" },
      h6: { fontWeight: 600, fontSize: "1rem" },
      subtitle1: { fontWeight: 500 },
      subtitle2: { fontWeight: 600 },
      body1: { lineHeight: 1.65 },
      body2: { lineHeight: 1.6 },
      button: { fontWeight: 600 },
    },
    shape: { borderRadius: 12 },
    customShadows: s,
    components: {
      MuiCssBaseline: {
        styleOverrides: {
          body: {
            backgroundColor: isDark ? "#141210" : "#f5f0ea",
            colorScheme: mode,
          },
          // Smoother scrollbars
          "*::-webkit-scrollbar": { width: 6 },
          "*::-webkit-scrollbar-track": { background: "transparent" },
          "*::-webkit-scrollbar-thumb": {
            background: isDark ? "rgba(255,255,255,0.1)" : "rgba(0,0,0,0.1)",
            borderRadius: 3,
          },
          "*::-webkit-scrollbar-thumb:hover": {
            background: isDark ? "rgba(255,255,255,0.18)" : "rgba(0,0,0,0.18)",
          },
          // Accessibility — a visible focus ring on keyboard-focused
          // interactive elements. MUI's ButtonBase resets `outline: 0` and
          // relies on a faint focus ripple, which many surfaces here hide, so
          // Tab left no visible trace. :focus-visible keeps mouse clicks from
          // drawing it. Components with a bespoke focus style still win on
          // specificity.
          "a:focus-visible, button:focus-visible, [role='button']:focus-visible, [tabindex]:focus-visible": {
            outline: `2px solid ${alpha(P, isDark ? 0.9 : 0.8)}`,
            outlineOffset: 2,
            borderRadius: 8,
          },
        },
      },
      MuiButton: {
        defaultProps: {
          disableElevation: true,
        },
        styleOverrides: {
          root: {
            textTransform: "none",
            fontWeight: 600,
            fontSize: "0.85rem",
            borderRadius: 10,
            boxShadow: "none",
            padding: "6px 18px",
            transition: "all 0.15s ease",
            "&:hover": {
              boxShadow: "none",
              transform: "translateY(-1px)",
            },
            "&:active": {
              transform: "translateY(0)",
            },
          },
          sizeSmall: {
            fontSize: "0.8rem",
            padding: "4px 14px",
            borderRadius: 8,
          },
          contained: {
            boxShadow: s.sm,
            "&:hover": { boxShadow: s.md },
          },
          containedPrimary: {
            background: `linear-gradient(135deg, ${P}, #e88a5a)`,
            // The gradient is a background-IMAGE, so MUI's disabled colour
            // never showed through: a disabled primary button looked enabled
            // on every page.
            "&.Mui-disabled": {
              background: isDark ? "rgba(255,255,255,0.12)" : "rgba(0,0,0,0.12)",
              color: isDark ? "rgba(255,255,255,0.38)" : "rgba(0,0,0,0.38)",
              boxShadow: "none",
            },
            "&:hover": {
              background: `linear-gradient(135deg, #b85838, #d47a4e)`,
              boxShadow: `${s.md}, ${s.glow}`,
            },
          },
          outlined: {
            borderColor: isDark ? "rgba(255,255,255,0.12)" : "rgba(0,0,0,0.12)",
            "&:hover": {
              borderColor: P,
              backgroundColor: `rgba(201,100,66,0.04)`,
            },
          },
          text: {
            padding: "4px 12px",
            "&:hover": {
              backgroundColor: `rgba(201,100,66,0.06)`,
            },
          },
        },
      },
      MuiPaper: {
        styleOverrides: {
          root: {
            backgroundImage: "none",
          },
          elevation1: { boxShadow: s.sm },
          elevation2: { boxShadow: s.md },
          elevation4: { boxShadow: s.lg },
          elevation8: { boxShadow: s.xl },
        },
      },
      MuiDrawer: {
        styleOverrides: {
          paper: {
            backgroundColor: isDark ? "rgba(20,18,16,0.88)" : "rgba(255,255,255,0.8)",
            backdropFilter: "blur(20px) saturate(1.4)",
            WebkitBackdropFilter: "blur(20px) saturate(1.4)",
            borderRight: `1px solid ${isDark ? "rgba(255,255,255,0.05)" : "rgba(0,0,0,0.06)"}`,
          },
        },
      },
      MuiCard: {
        styleOverrides: {
          root: {
            backgroundImage: "none",
            border: `1px solid ${isDark ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.06)"}`,
            borderRadius: 14,
            boxShadow: s.sm,
            transition: "all 0.2s cubic-bezier(0.4, 0, 0.2, 1)",
            "&:hover": {
              boxShadow: `${s.lg}, ${s.glow}`,
              borderColor: isDark ? "rgba(201,100,66,0.2)" : "rgba(201,100,66,0.15)",
              transform: "translateY(-2px)",
            },
          },
        },
      },
      MuiChip: {
        styleOverrides: {
          root: { fontWeight: 500, borderRadius: 8 },
          // A filled status chip is white text on the palette colour, and
          // `success.main` (#16a34a) gives white only 3.30:1 — under the 4.5:1
          // floor for text this small. Measured on /scout, where a whole grid
          // of "Downloaded" chips sits on one screen, and it reads the same in
          // both themes because the palette entry has no per-mode variant.
          //
          // Fixed HERE and not on `success.main`, which is also drawn as text
          // and as icons on the dark canvas — darkening the palette would fix
          // the chip and make those worse.
          filledSuccess: { backgroundColor: "#12883e" },   // 4.55:1 vs white
          outlined: {
            borderColor: isDark ? "rgba(255,255,255,0.12)" : "rgba(0,0,0,0.1)",
            transition: "all 0.15s ease",
            "&:hover": {
              borderColor: P,
              backgroundColor: "rgba(201,100,66,0.06)",
            },
          },
        },
      },
      MuiTextField: {
        styleOverrides: {
          root: {
            "& .MuiOutlinedInput-root": {
              borderRadius: 12,
              transition: "all 0.15s ease",
              "& fieldset": {
                borderColor: isDark ? "rgba(255,255,255,0.1)" : "rgba(0,0,0,0.1)",
                transition: "all 0.15s ease",
              },
              "&:hover fieldset": {
                borderColor: isDark ? "rgba(255,255,255,0.2)" : "rgba(0,0,0,0.2)",
              },
              "&.Mui-focused": {
                backgroundColor: isDark ? "rgba(201,100,66,0.03)" : "rgba(201,100,66,0.02)",
              },
              "&.Mui-focused fieldset": {
                borderColor: P,
                borderWidth: 2,
              },
            },
          },
        },
      },
      MuiSelect: {
        styleOverrides: {
          root: { borderRadius: 12 },
        },
      },
      MuiDialog: {
        styleOverrides: {
          paper: {
            borderRadius: 20,
            boxShadow: isDark ? "0 12px 40px rgba(0,0,0,0.6)" : "0 12px 40px rgba(0,0,0,0.12)",
            border: `1px solid ${isDark ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.06)"}`,
          },
          backdrop: {
            backdropFilter: "blur(8px)",
            WebkitBackdropFilter: "blur(8px)",
            backgroundColor: isDark ? "rgba(0,0,0,0.5)" : "rgba(0,0,0,0.25)",
          },
        },
      },
      MuiLinearProgress: {
        styleOverrides: {
          root: { borderRadius: 4, height: 6 },
          bar: { borderRadius: 4 },
        },
      },
      MuiSwitch: {
        styleOverrides: {
          switchBase: {
            "&.Mui-checked": { color: P },
            "&.Mui-checked + .MuiSwitch-track": { backgroundColor: P },
          },
        },
      },
      MuiListItemButton: {
        styleOverrides: {
          root: {
            borderRadius: 10,
            transition: "all 0.15s ease",
            "&.Mui-selected": {
              backgroundColor: "rgba(201,100,66,0.08)",
              "&:hover": { backgroundColor: "rgba(201,100,66,0.12)" },
            },
          },
        },
      },
      MuiTabs: {
        styleOverrides: {
          root: { minHeight: 40 },
          indicator: {
            backgroundColor: P,
            borderRadius: 3,
            height: 3,
          },
        },
      },
      MuiTab: {
        styleOverrides: {
          root: {
            textTransform: "none",
            fontWeight: 500,
            fontSize: "0.9rem",
            minHeight: 40,
            padding: "8px 16px",
            borderRadius: "10px 10px 0 0",
            transition: "all 0.15s ease",
            "&.Mui-selected": {
              color: P,
              fontWeight: 700,
              backgroundColor: isDark ? "rgba(201,100,66,0.06)" : "rgba(201,100,66,0.04)",
            },
          },
        },
      },
      // Segmented controls had NO theme entry, so every call site styled
      // itself and the same widget rendered several different ways on one
      // screen. Giving the component a home here is the same rule the caption
      // enum and the tool job-type map follow: one source, and a call site
      // that disagrees is now visibly disagreeing.
      MuiToggleButtonGroup: {
        styleOverrides: {
          root: { borderRadius: 10 },
          grouped: {
            borderRadius: 10,
            // MUI zeroes the inner corners of a group; restore them so the
            // ends stay rounded and the seams stay square.
            "&:not(:first-of-type)": { borderTopLeftRadius: 0, borderBottomLeftRadius: 0 },
            "&:not(:last-of-type)": { borderTopRightRadius: 0, borderBottomRightRadius: 0 },
          },
        },
      },
      MuiToggleButton: {
        styleOverrides: {
          root: {
            textTransform: "none",
            fontWeight: 600,
            borderRadius: 10,
          },
        },
      },
      MuiDivider: {
        styleOverrides: {
          root: { borderColor: isDark ? "rgba(255,255,255,0.05)" : "rgba(0,0,0,0.05)" },
        },
      },
      MuiTooltip: {
        styleOverrides: {
          tooltip: {
            backgroundColor: isDark ? "#3a3735" : "#2d2b28",
            borderRadius: 8,
            fontSize: "0.8rem",
            padding: "6px 12px",
            boxShadow: s.lg,
          },
          arrow: {
            color: isDark ? "#3a3735" : "#2d2b28",
          },
        },
      },
      MuiAlert: {
        styleOverrides: {
          root: { borderRadius: 12 },
          filledSuccess: { background: "linear-gradient(135deg, #16a34a, #22c55e)" },
          filledError: { background: "linear-gradient(135deg, #dc2626, #ef4444)" },
          filledWarning: { background: "linear-gradient(135deg, #d97706, #f59e0b)" },
          filledInfo: { background: "linear-gradient(135deg, #2563eb, #3b82f6)" },
        },
      },
      MuiSnackbar: {
        styleOverrides: {
          root: { "& .MuiPaper-root": { borderRadius: 12, boxShadow: s.xl } },
        },
      },
    },
  })

  return theme
}
