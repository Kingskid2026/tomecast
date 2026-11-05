// A voice in the voice menu: a 44 px pill in Plex Sans, filled with the text colour when chosen.
import QtQuick
import QtQuick.Templates as T
import ".."

T.AbstractButton {
    id: control

    property bool chosen: false
    // Corner-window size: 36 px tall, 13 px type.
    property bool compact: false

    // Fractional text width plus padding and the 1 px border on each side, like the mockup's
    // CSS, so a row of voices wraps where the mockup's does.
    implicitWidth: metrics.advanceWidth + leftPadding + rightPadding + 2
    implicitHeight: compact ? 36 : Theme.controlHeight
    leftPadding: compact ? 14 : 18
    rightPadding: leftPadding
    focusPolicy: Qt.StrongFocus
    hoverEnabled: true

    font.family: Theme.sans
    font.pixelSize: compact ? 13 : 14

    Accessible.role: Accessible.RadioButton
    Accessible.name: text
    Accessible.checkable: true
    Accessible.checked: chosen
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
        color: control.chosen ? Theme.bg : Theme.text
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        textFormat: Text.PlainText
    }

    background: Rectangle {
        radius: height / 2
        color: control.chosen ? Theme.text : "transparent"
        border.width: 1
        border.color: control.chosen || control.hovered ? Theme.text : Theme.line

        Behavior on color {
            enabled: Theme.motion
            ColorAnimation { duration: Theme.quickMs }
        }

        FocusRing {
            target: control
            cornerRadius: parent.radius
        }
    }
}
