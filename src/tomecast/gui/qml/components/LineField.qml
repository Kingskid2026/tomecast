// A text field drawn as text over one 1 px line, no box (the Names screen's "say it as" and
// TRY ANY WORD fields): 44 px tall, Plex Mono 15, with a muted placeholder. Focus from the
// keyboard shows the accent ring, as on every other control.
import QtQuick
import QtQuick.Templates as T
import ".."

T.TextField {
    id: control

    property color lineColor: Theme.line
    // Like visualFocus on buttons: the ring shows when focus came from the keyboard.
    readonly property bool keyboardFocus: activeFocus
                                          && (focusReason === Qt.TabFocusReason
                                              || focusReason === Qt.BacktabFocusReason
                                              || focusReason === Qt.ShortcutFocusReason)

    implicitWidth: 120
    implicitHeight: Theme.controlHeight
    leftPadding: 2
    rightPadding: 2
    topPadding: 0
    bottomPadding: 0
    verticalAlignment: TextInput.AlignVCenter
    color: Theme.text
    selectionColor: Theme.accent
    selectedTextColor: Theme.textOnAccent
    placeholderTextColor: Theme.muted
    font.family: Theme.mono
    font.pixelSize: 15
    selectByMouse: true
    Accessible.role: Accessible.EditableText
    Accessible.name: placeholderText

    Text {
        x: control.leftPadding
        width: control.width - control.leftPadding - control.rightPadding
        height: control.height
        verticalAlignment: Text.AlignVCenter
        visible: control.length === 0 && control.preeditText === ""
        text: control.placeholderText
        color: control.placeholderTextColor
        font: control.font
        elide: Text.ElideRight
        textFormat: Text.PlainText
    }

    background: Item {
        Rectangle {
            anchors.bottom: parent.bottom
            width: parent.width
            height: 1
            color: control.lineColor
        }

        Rectangle {
            anchors.fill: parent
            anchors.margins: -(3 + Theme.focusWidth)
            color: "transparent"
            border.width: Theme.focusWidth
            border.color: Theme.accent
            visible: control.keyboardFocus
            z: 10
        }
    }
}
