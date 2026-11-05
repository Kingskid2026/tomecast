// A 44 px pill with a mono caps label. Variants: "outline" (hairline border), "strong" (text
// coloured border, as on the drop screen) and "filled" (the accent, for the primary action).
import QtQuick
import QtQuick.Templates as T
import ".."

T.AbstractButton {
    id: control

    property string variant: "outline"
    readonly property bool filled: variant === "filled"
    readonly property int textSize: 12
    // Letter spacing in em (0.14 on the drop screen's Choose a book).
    property real tracking: 0.12

    // Fractional text width plus padding and the 1 px border on each side, like the mockup's CSS.
    implicitWidth: Math.max(Theme.controlHeight,
                            metrics.advanceWidth + leftPadding + rightPadding + (filled ? 0 : 2))
    implicitHeight: Theme.controlHeight
    leftPadding: variant === "outline" ? 20 : 22
    rightPadding: leftPadding
    focusPolicy: Qt.StrongFocus
    hoverEnabled: true
    opacity: enabled ? 1 : 0.4

    font.family: Theme.mono
    font.pixelSize: textSize
    font.weight: Font.Medium
    font.letterSpacing: textSize * tracking
    font.capitalization: Font.AllUppercase

    Accessible.role: Accessible.Button
    Accessible.name: text
    Accessible.onPressAction: control.clicked()

    TextMetrics {
        id: metrics
        font: control.font
        text: control.text
    }

    contentItem: Text {
        id: label
        text: control.text
        font: control.font
        color: control.filled ? Theme.textOnAccent : Theme.text
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        textFormat: Text.PlainText
        elide: Text.ElideRight
    }

    background: Rectangle {
        radius: height / 2
        color: control.filled ? Theme.accent : "transparent"
        border.width: control.filled ? 0 : 1
        border.color: control.variant === "strong" || control.hovered || control.down ? Theme.text : Theme.line
        opacity: control.down ? 0.8 : 1

        Behavior on border.color {
            enabled: Theme.motion
            ColorAnimation { duration: Theme.quickMs }
        }

        FocusRing {
            target: control
            cornerRadius: parent.radius
        }
    }
}
