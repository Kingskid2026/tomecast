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

    // The same design reflows in a narrow window (often parked in a corner): at this width or
    // less, from the mockups' `@container tc (max-width: 680px)` rules. The window goes down
    // to 400 x 560.
    readonly property int narrowMaxWidth: 680
    readonly property int narrowPadTop: 20
    readonly property int narrowPadSide: 20
    readonly property int narrowPadBottom: 24
    readonly property int narrowGap: 20
    // Every dot outside the cover and the Cantoria is 5 px (Doto's dots are 0.075 em: 67 px).
    readonly property int dotSize: 5
    readonly property int dotGap: 3
    readonly property int bigNumber: 67

    // Controls.
    readonly property int controlHeight: 44
    readonly property int roundButtonSize: 56
    readonly property int focusWidth: 2

    // Motion: state changes only, 150-400 ms, none with reduced motion.
    readonly property bool motion: !Backend.reduceMotion
    readonly property int fadeMs: 200
    readonly property int quickMs: 150
}
