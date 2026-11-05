// The top bar: TOMECAST, "[ 0N ] SCREEN", and a right-hand slot (buttons or a status line).
// The slot moves under the wordmark when the window is too narrow for both.
import QtQuick
import QtQuick.Layouts
import ".."

SplitRow {
    id: header

    // Screen number for "[ 0N ]"; with an empty title only the wordmark shows (drop screen).
    property int index: 0
    property string title: ""
    // Corner-window sizes: 12 px wordmark, 10 px labels, 12 px apart.
    property bool compact: false
    default property alias trailing: trailingRow.data

    objectName: "header"
    gap: compact ? 8 : 12
    Accessible.role: Accessible.Pane
    Accessible.name: title === "" ? "Tomecast" : "Tomecast, step " + index + ", " + title

    // Wordmark and "[ 0N ] SCREEN", sharing a baseline.
    Item {
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignVCenter
        implicitWidth: titleLabel.visible ? titleLabel.x + titleLabel.implicitWidth : wordmark.implicitWidth
        implicitHeight: wordmark.implicitHeight

        MonoLabel {
            id: wordmark
            objectName: "wordmark"
            text: "TOMECAST"
            size: header.compact ? 12 : 13
            font.weight: Font.Medium
            tracking: 0.16
            color: Theme.text
        }

        MonoLabel {
            id: titleLabel
            objectName: "screenTitle"
            x: wordmark.implicitWidth + (header.compact ? 12 : 20)
            y: wordmark.baselineOffset - baselineOffset
            visible: header.title !== ""
            text: "[ " + (header.index < 10 ? "0" : "") + header.index + " ] " + header.title.toUpperCase()
            size: header.compact ? 10 : 11
            tracking: 0.16
        }
    }

    Row {
        id: trailingRow
        Layout.alignment: header.stacked ? Qt.AlignLeft | Qt.AlignVCenter : Qt.AlignRight | Qt.AlignVCenter
        spacing: header.compact ? 8 : 10
    }
}
