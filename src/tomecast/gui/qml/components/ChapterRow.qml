// One section in the chapter list (a ChapterModel row): tick, "[ 03 ]", title, words, minutes
// and a 17-dot length meter. The corner window keeps the tick, number, title and minutes in a
// 36 px row. Clicking it ticks or unticks the section through the backend.
import QtQuick
import QtQuick.Layouts
import QtQuick.Templates as T
import TomecastNative
import ".."

T.AbstractButton {
    id: row

    required property int index
    required property string number
    required property string title
    required property string wordsLabel
    required property string minutesLabel
    required property int meterLit
    required property bool frontMatter
    required property bool selected

    property bool compact: false
    readonly property color ink: selected ? Theme.text : Theme.muted
    // In a narrower wide layout the length meter, then the word count, make way for titles
    // (both stay in the accessible description).
    readonly property bool showMeter: !compact && width >= 600
    readonly property bool showWords: !compact && width >= 480

    objectName: "chapterRow" + index
    // 48 px (36 px compact) plus the 1 px rule under it, as in the mockups.
    implicitHeight: compact ? 36 : Math.max(49, cells.implicitHeight + 9)
    focusPolicy: Qt.StrongFocus
    hoverEnabled: true
    Accessible.role: Accessible.CheckBox
    Accessible.name: "Include " + title
    Accessible.description: wordsLabel + " words, " + minutesLabel
    Accessible.checkable: true
    Accessible.checked: selected
    Accessible.onPressAction: row.clicked()
    Accessible.onToggleAction: row.clicked()
    onClicked: Backend.setChapterSelected(index, !selected)

    background: Item {
        Rectangle {
            anchors.bottom: parent.bottom
            width: parent.width
            height: 1
            color: Theme.line
        }

        FocusRing {
            target: row
            gap: -2
        }
    }

    contentItem: RowLayout {
        id: cells
        spacing: row.compact ? 8 : 12

        Item {
            Layout.preferredWidth: row.compact ? 18 : 28
            Layout.preferredHeight: tick.height

            Tick {
                id: tick
                anchors.verticalCenter: parent.verticalCenter
                size: row.compact ? 16 : 18
                checked: row.selected
            }
        }

        MonoLabel {
            Layout.preferredWidth: row.compact ? 44 : 64
            text: row.number
            size: row.compact ? 10 : 12
            tracking: 0.06
        }

        // Title, elided to fit, then the FRONT MATTER tag on its baseline (wide only).
        Item {
            id: titleCell
            readonly property real tagRoom: frontTag.visible ? 10 + frontTag.implicitWidth : 0

            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.preferredWidth: chapterTitle.implicitWidth + tagRoom
            implicitHeight: chapterTitle.implicitHeight

            Text {
                id: chapterTitle
                objectName: "chapterTitle"
                width: Math.max(0, Math.min(implicitWidth, titleCell.width - titleCell.tagRoom))
                anchors.verticalCenter: parent.verticalCenter
                text: row.title
                color: row.ink
                font.family: Theme.sans
                font.pixelSize: row.compact ? 13 : 15
                elide: Text.ElideRight
                textFormat: Text.PlainText
            }

            MonoLabel {
                id: frontTag
                visible: row.frontMatter && !row.compact
                x: chapterTitle.width + 10
                y: chapterTitle.y + chapterTitle.baselineOffset - baselineOffset
                text: "FRONT MATTER"
                size: 10
                tracking: 0.12
            }
        }

        MonoLabel {
            visible: row.showWords
            Layout.preferredWidth: 84
            horizontalAlignment: Text.AlignRight
            text: row.wordsLabel
            size: 12
            tracking: 0
        }

        MonoLabel {
            Layout.preferredWidth: row.compact ? 46 : 64
            horizontalAlignment: Text.AlignRight
            text: row.minutesLabel
            size: row.compact ? 10 : 12
            tracking: 0
        }

        Item {
            visible: row.showMeter
            Layout.preferredWidth: 136
            Layout.preferredHeight: 6

            DotMeter {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                count: 17
                lit: row.meterLit
                dotSize: 6
                dotSpacing: 2
                litColor: row.ink
                Accessible.ignored: true
            }
        }
    }
}
