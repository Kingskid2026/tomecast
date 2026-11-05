// The top bar: TOMECAST, the screen's name, and a right-hand slot (buttons or a status line).
// The slot moves under the wordmark when the window is too narrow for both.
import QtQuick
import QtQuick.Layouts
import ".."

SplitRow {
    id: header

    // The screen's name next to the wordmark ("CHAPTERS"); empty on the drop screen.
    property string title: ""
    default property alias trailing: trailingRow.data

    objectName: "header"
    gap: 12
    Accessible.role: Accessible.Pane
    Accessible.name: title === "" ? "Tomecast" : "Tomecast, " + title

    // Wordmark and screen name, sharing a baseline.
    Item {
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignVCenter
        implicitWidth: titleLabel.visible ? titleLabel.x + titleLabel.implicitWidth : wordmark.implicitWidth
        implicitHeight: wordmark.implicitHeight

        MonoLabel {
            id: wordmark
            objectName: "wordmark"
            text: "TOMECAST"
            size: 13
            font.weight: Font.Medium
            tracking: 0.16
            color: Theme.text
        }

        MonoLabel {
            id: titleLabel
            objectName: "screenTitle"
            x: wordmark.implicitWidth + 20
            y: wordmark.baselineOffset - baselineOffset
            visible: header.title !== ""
            text: header.title.toUpperCase()
            size: 11
            tracking: 0.16
        }
    }

    Row {
        id: trailingRow
        Layout.alignment: header.stacked ? Qt.AlignLeft | Qt.AlignVCenter : Qt.AlignRight | Qt.AlignVCenter
        spacing: 10
    }
}
