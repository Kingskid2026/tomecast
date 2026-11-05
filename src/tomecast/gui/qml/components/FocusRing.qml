// The 2 px accent outline every focusable control shows when it has keyboard focus.
import QtQuick
import ".."

Rectangle {
    id: ring

    // The control this ring belongs to (usually the parent).
    property Item target: parent
    property real gap: 3
    property real cornerRadius: 0

    anchors.fill: parent
    anchors.margins: -(gap + Theme.focusWidth)
    radius: cornerRadius > 0 ? cornerRadius + gap + Theme.focusWidth : 0
    color: "transparent"
    border.width: Theme.focusWidth
    border.color: Theme.accent
    visible: target !== null && target.visualFocus === true
    z: 10
}
