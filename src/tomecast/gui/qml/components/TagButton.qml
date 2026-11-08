// A 36 px button with a mono caps label, smaller than PillButton: the Names screen's
// THIS BOOK / EVERY BOOK pill, and (plain: no outline, muted) its REMOVE.
import QtQuick
import QtQuick.Templates as T
import ".."

T.AbstractButton {
    id: control

    property bool plain: false
    readonly property int textSize: 11

    // Fractional text width plus padding and the 1 px border on each side, like the mockup's CSS.
    implicitWidth: metrics.advanceWidth + leftPadding + rightPadding + (plain ? 0 : 2)
    implicitHeight: 36
    leftPadding: plain ? 10 : 14
    rightPadding: leftPadding
    focusPolicy: Qt.StrongFocus
    hoverEnabled: true

    font.family: Theme.mono
    font.pixelSize: textSize
    font.letterSpacing: textSize * 0.12
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
        text: control.text
        font: control.font
        color: !control.plain || control.hovered || control.down ? Theme.text : Theme.muted
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        textFormat: Text.PlainText
    }

    background: Rectangle {
        radius: height / 2
        color: "transparent"
        border.width: control.plain ? 0 : 1
        border.color: control.hovered || control.down ? Theme.text : Theme.line
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
