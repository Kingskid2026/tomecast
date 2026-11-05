// A problem to show above the current screen (Backend.errorMessage), with a way to dismiss it.
import QtQuick
import QtQuick.Layouts
import QtQuick.Templates as T
import ".."

T.Control {
    id: banner

    property string message: ""
    signal dismissed()

    implicitWidth: contentItem.implicitWidth + leftPadding + rightPadding
    implicitHeight: contentItem.implicitHeight + topPadding + bottomPadding
    leftPadding: width < Theme.compactBelow ? Theme.compactPadH : Theme.pageSide
    rightPadding: leftPadding
    topPadding: 12
    bottomPadding: 12

    Accessible.role: Accessible.AlertMessage
    Accessible.name: message

    background: Rectangle {
        color: Theme.surface
        Rectangle {
            anchors.bottom: parent.bottom
            width: parent.width
            height: 1
            color: Theme.line
        }
    }

    contentItem: SplitRow {
        gap: 16

        Text {
            objectName: "errorText"
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignVCenter
            text: banner.message
            color: Theme.text
            font.family: Theme.sans
            font.pixelSize: 14
            wrapMode: Text.Wrap
            textFormat: Text.PlainText
        }

        PillButton {
            objectName: "dismissErrorButton"
            text: qsTr("Dismiss")
            onClicked: banner.dismissed()
        }
    }
}
