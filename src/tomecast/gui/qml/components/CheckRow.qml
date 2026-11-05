// A tick box with a label, 44 px tall: the skip switches.
// It doesn't toggle itself: bind `checked` to the backend and act in onClicked.
import QtQuick
import QtQuick.Templates as T
import ".."

T.AbstractButton {
    id: control

    implicitWidth: implicitContentWidth + leftPadding + rightPadding
    implicitHeight: Math.max(Theme.controlHeight, implicitContentHeight)
    focusPolicy: Qt.StrongFocus
    checkable: false
    hoverEnabled: true
    spacing: 10

    font.family: Theme.sans
    font.pixelSize: 14

    Accessible.role: Accessible.CheckBox
    Accessible.name: text
    Accessible.checkable: true
    Accessible.checked: checked
    Accessible.onPressAction: control.clicked()
    Accessible.onToggleAction: control.clicked()

    contentItem: Row {
        spacing: control.spacing

        Tick {
            anchors.verticalCenter: parent.verticalCenter
            size: 18
            checked: control.checked
        }

        Text {
            anchors.verticalCenter: parent.verticalCenter
            text: control.text
            font: control.font
            color: Theme.text
            textFormat: Text.PlainText
        }
    }

    FocusRing {
        target: control
        gap: 1
        cornerRadius: 4
    }
}
