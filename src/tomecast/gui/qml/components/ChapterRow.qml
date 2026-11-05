// One section in the chapter list (a ChapterModel row): tick, title, words, minutes and a
// 17-dot length meter, as in Chapters.dc.html. In a narrow window (680 px or less) the word
// count and the meter are dropped, leaving tick, title (elided) and minutes, as in the
// mockup's narrow rules. Clicking the row ticks or unticks the section through the backend.
import QtQuick
import QtQuick.Layouts
import QtQuick.Templates as T
import TomecastNative
import ".."

T.AbstractButton {
    id: row

    required property int index
    required property string title
    required property string wordsLabel
    required property string minutesLabel
    required property int meterLit
    required property bool frontMatter
    required property bool selected

    // Set by the screen: the window is 680 px wide or less.
    property bool narrow: false
    readonly property color ink: selected ? Theme.text : Theme.muted
    // The word count and meter stay in the accessible description when they're hidden.
    readonly property bool showWords: !narrow
    readonly property bool showMeter: !narrow

    objectName: "chapterRow" + index
    // 48 px plus the 1 px rule under it, as in the mockups.
    implicitHeight: Math.max(49, cells.implicitHeight + 9)
    focusPolicy: Qt.StrongFocus
    hoverEnabled: true
    Accessible.role: Accessible.CheckBox
    Accessible.name: "Include " + (title === "Untitled section" ? "untitled section" : title)
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
        spacing: row.narrow ? 10 : 12

        Item {
            Layout.preferredWidth: row.narrow ? 24 : 28
            Layout.preferredHeight: tick.height

            Tick {
                id: tick
                anchors.verticalCenter: parent.verticalCenter
                size: 18
                checked: row.selected
            }
        }

        // Title, elided to fit, then the FRONT MATTER tag on its baseline.
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
                font.pixelSize: 15
                elide: Text.ElideRight
                textFormat: Text.PlainText
            }

            MonoLabel {
                id: frontTag
                visible: row.frontMatter
                x: chapterTitle.width + 10
                y: chapterTitle.y + chapterTitle.baselineOffset - baselineOffset
                text: "FRONT MATTER"
                size: 10
                tracking: 0.12
            }
        }

        MonoLabel {
            objectName: "chapterWords"
            visible: row.showWords
            Layout.preferredWidth: 84
            horizontalAlignment: Text.AlignRight
            text: row.wordsLabel
            size: 12
            tracking: 0
        }

        MonoLabel {
            objectName: "chapterMinutes"
            Layout.preferredWidth: row.narrow ? 56 : 64
            horizontalAlignment: Text.AlignRight
            text: row.minutesLabel
            size: 12
            tracking: 0
        }

        Item {
            objectName: "chapterMeter"
            visible: row.showMeter
            Layout.preferredWidth: 136
            Layout.preferredHeight: Theme.dotSize

            DotMeter {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                count: 17
                lit: row.meterLit
                dotSize: Theme.dotSize
                dotSpacing: Theme.dotGap
                litColor: row.ink
                Accessible.ignored: true
            }
        }
    }
}
