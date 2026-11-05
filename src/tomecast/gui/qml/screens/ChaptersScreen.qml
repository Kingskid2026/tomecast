// Chapters: the cover and summary, the skip switch, and a spec table of sections
// (Chapters.dc.html). Narrow windows drop the word count and the meter from the rows.
import QtQuick
import QtQuick.Layouts
import TomecastNative
import ".."
import "../components"

ScreenPage {
    id: page

    // With nothing ticked the backend says "NO AUDIO" (and "NOTHING TO MAKE").
    readonly property bool nothingSelected: Backend.selectionSummary.audio === "NO AUDIO"
                                            || Backend.selectionSummary.make === "NOTHING TO MAKE"
    readonly property string coverDescription: "Cover of " + Backend.bookTitle + ", shown as dots"

    objectName: "chaptersScreen"
    designHeight: 980
    Accessible.role: Accessible.Pane
    Accessible.name: "Chapters"

    Header {
        Layout.fillWidth: true
        title: "Chapters"

        PillButton {
            objectName: "chaptersBackButton"
            text: "Back"
            onClicked: Backend.goTo("drop")
        }

        PillButton {
            objectName: "chooseVoiceButton"
            variant: "filled"
            text: "Choose a voice"
            enabled: !page.nothingSelected
            onClicked: Backend.goTo("voice")
        }
    }

    GridLayout {
        id: body
        readonly property bool sideBySide: page.innerWidth >= 240 + Theme.columnGap + 560

        Layout.fillWidth: true
        columns: sideBySide ? 2 : 1
        columnSpacing: Theme.columnGap
        rowSpacing: Theme.columnGap

        // The book: cover, title, author and what's selected.
        ColumnLayout {
            Layout.alignment: Qt.AlignTop
            Layout.fillWidth: !body.sideBySide
            Layout.preferredWidth: 240
            Layout.maximumWidth: 260
            spacing: 16

            DotImage {
                objectName: "chaptersCover"
                visible: Backend.coverSource !== ""
                source: Backend.coverSource
                displayWidth: 240
                placeholderHeight: 331
                description: page.coverDescription
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 4

                Text {
                    objectName: "bookTitle"
                    Layout.fillWidth: true
                    text: Backend.bookTitle
                    color: Theme.text
                    font.family: Theme.sans
                    font.pixelSize: 20
                    font.weight: Font.Medium
                    lineHeightMode: Text.FixedHeight
                    lineHeight: 26
                    wrapMode: Text.Wrap
                    textFormat: Text.PlainText
                }

                Text {
                    objectName: "bookAuthors"
                    Layout.fillWidth: true
                    text: Backend.bookAuthors
                    color: Theme.muted
                    font.family: Theme.sans
                    font.pixelSize: 15
                    wrapMode: Text.Wrap
                    textFormat: Text.PlainText
                }
            }

            ColumnLayout {
                objectName: "selectionSummary"
                Layout.fillWidth: true
                spacing: 6

                Repeater {
                    model: ["sections", "words", "audio", "make"]
                    delegate: MonoLabel {
                        required property string modelData
                        objectName: "summary_" + modelData
                        Layout.fillWidth: true
                        text: Backend.selectionSummary[modelData] || ""
                        tracking: 0.1
                        wrapMode: Text.Wrap
                    }
                }
            }
        }

        // The switch and the table.
        ColumnLayout {
            Layout.alignment: Qt.AlignTop
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            spacing: 20

            CheckRow {
                objectName: "skipFrontMatterSwitch"
                text: "Skip front matter"
                checked: Backend.skipFrontMatter
                onClicked: Backend.setSkipFrontMatter(!Backend.skipFrontMatter)
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0

                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: 1
                    color: Theme.line
                }

                ListView {
                    objectName: "chapterList"
                    Layout.fillWidth: true
                    implicitHeight: contentHeight
                    interactive: false
                    model: Backend.chapters
                    keyNavigationEnabled: false
                    Accessible.role: Accessible.List
                    Accessible.name: "Sections of the book"
                    delegate: ChapterRow {
                        width: ListView.view.width
                        narrow: page.narrow
                    }
                }
            }
        }
    }

    // Spare height stays at the bottom.
    Item { Layout.fillHeight: true; Layout.topMargin: -page.sectionGap }
}
