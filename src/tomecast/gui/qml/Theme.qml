pragma Singleton

// Night Studio palettes, type and spacing (docs/design.md). Follows Backend.theme.
import QtQuick
import TomecastNative

QtObject {
    readonly property bool dark: Backend.theme !== "light"

    // Palette. Light values other than bg, text and accent are placeholders (design.md).
    readonly property color bg: dark ? "#0B0B0C" : "#F2F2F2"
    readonly property color surface: dark ? "#161618" : "#FFFFFF"
    readonly property color line: dark ? "#2A2A2E" : "#D9D9DE"
    readonly property color text: dark ? "#F2F2F2" : "#0B0B0C"
    readonly property color muted: dark ? "#7E7E7E" : "#5E5E66"
    readonly property color accent: dark ? "#7C8CFF" : "#4A5CF2"
    readonly property color textOnAccent: dark ? "#0B0B0C" : "#FFFFFF"
    readonly property color wait: dark ? "#2A2A2E" : "#D0D0D6"

    // Type. Doto is for display numerals only (24 px and up).
    readonly property string display: "Doto"
    readonly property string sans: "IBM Plex Sans"
    readonly property string mono: "IBM Plex Mono"

    // Page spacing, from the mockups.
    readonly property int pageTop: 28
    readonly property int pageSide: 40
    readonly property int pageBottom: 36
    readonly property int sectionGap: 28
    readonly property int columnGap: 40

    // Below this window width the screens use the compact "corner window" layout (the window
    // is often small, parked in a corner; mockups Corner-*.dc.html, drawn at 400 x 600).
    // The window itself goes down to 400 x 560.
    readonly property int compactBelow: 640
    readonly property int compactPadV: 16
    readonly property int compactPadH: 18
    readonly property int compactGap: 14

    // Controls.
    readonly property int controlHeight: 44
    readonly property int roundButtonSize: 56
    readonly property int focusWidth: 2

    // Motion: state changes only, 150-400 ms, none with reduced motion.
    readonly property bool motion: !Backend.reduceMotion
    readonly property int fadeMs: 200
    readonly property int quickMs: 150
}
