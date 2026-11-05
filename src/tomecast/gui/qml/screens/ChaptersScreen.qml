// [ 02 ] Chapters: the cover and summary, the skip switch, and a spec table of sections.
// Wide: Chapters.dc.html. Compact (corner window): Corner-chapters.dc.html, where the list
// scrolls by itself between the book and the buttons.
import QtQuick
import QtQuick.Controls.Basic as Controls
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
    wideDesignHeight: 980
    Accessible.role: Accessible.Pane
    Accessible.name: "Chapters"

    wideView: ColumnLayout {
        spacing: Theme.sectionGap

        Header {
            Layout.fillWidth: true
            index: 2
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
                Layout.preferredWidth: 240
                Layout.maximumWidth: 240
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
                        }
                    }
                }
            }
        }

        // Spare height stays at the bottom.
        Item { Layout.fillHeight: true }
    }

    compactView: ColumnLayout {
        spacing: Theme.compactGap

        Header {
            Layout.fillWidth: true
            compact: true
            index: 2
            title: "Chapters"

            MonoLabel {
                objectName: "hardwareLabel"
                text: Backend.hardwareLabel.replace("ABOUT ", "")
                size: 10
            }
        }

        // The book: a small cover beside the title and author.
        RowLayout {
            Layout.fillWidth: true
            spacing: 16

            DotImage {
                objectName: "chaptersCover"
                Layout.alignment: Qt.AlignTop
                visible: Backend.coverSource !== ""
                source: Backend.coverSource
                displayWidth: 72
                placeholderHeight: 99
                description: page.coverDescription
            }

            ColumnLayout {
                Layout.alignment: Qt.AlignTop
                Layout.fillWidth: true
                Layout.topMargin: 2
                spacing: 4

                Text {
                    objectName: "bookTitle"
                    Layout.fillWidth: true
                    text: Backend.bookTitle
                    color: Theme.text
                    font.family: Theme.sans
                    font.pixelSize: 16
                    font.weight: Font.Medium
                    lineHeightMode: Text.FixedHeight
                    lineHeight: 20.8
                    wrapMode: Text.Wrap
                    maximumLineCount: 3
                    elide: Text.ElideRight
                    textFormat: Text.PlainText
                }

                Text {
                    objectName: "bookAuthors"
                    Layout.fillWidth: true
                    text: Backend.bookAuthors
                    color: Theme.muted
                    font.family: Theme.sans
                    font.pixelSize: 13
                    elide: Text.ElideRight
                    textFormat: Text.PlainText
                }
            }
        }

        // The sections, scrolling by themselves.
        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            Rectangle {
                Layout.fillWidth: true
                implicitHeight: 1
                color: Theme.line
            }

            ListView {
                objectName: "chapterList"
                Layout.fillWidth: true
                Layout.fillHeight: true
                // Three rows at least; the rest of the window when there's room.
                Layout.minimumHeight: 3 * 36
                Layout.preferredHeight: 3 * 36
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                model: Backend.chapters
                keyNavigationEnabled: false
                Accessible.role: Accessible.List
                Accessible.name: "Sections of the book"
                Controls.ScrollBar.vertical: Controls.ScrollBar {
                    contentItem: Rectangle {
                        implicitWidth: 3
                        radius: 1.5
                        color: Theme.muted
                        opacity: parent.active ? 0.8 : 0
                    }
                }
                delegate: ChapterRow {
                    width: ListView.view.width
                    compact: true
                }
            }
        }

        CheckRow {
            objectName: "skipFrontMatterSwitch"
            compact: true
            text: "Skip front matter"
            checked: Backend.skipFrontMatter
            onClicked: Backend.setSkipFrontMatter(!Backend.skipFrontMatter)
        }

        // "12 OF 15 SECTIONS · ABOUT 2 H 47 MIN" and "ABOUT 36 MIN TO MAKE HERE".
        ColumnLayout {
            objectName: "selectionSummary"
            Layout.fillWidth: true
            spacing: 0

            MonoLabel {
                objectName: "summary_line1"
                Layout.fillWidth: true
                text: {
                    const s = Backend.selectionSummary
                    const audio = (s.audio || "").replace(" OF AUDIO", "")
                    return (s.sections || "") + (audio !== "" ? " · " + audio : "")
                }
                size: 10
                tracking: 0.08
                lineHeightMode: Text.FixedHeight
                lineHeight: 15
                elide: Text.ElideRight
            }

            MonoLabel {
                objectName: "summary_line2"
                Layout.fillWidth: true
                text: Backend.selectionSummary.make || ""
                size: 10
                tracking: 0.08
                lineHeightMode: Text.FixedHeight
                lineHeight: 15
                elide: Text.ElideRight
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            PillButton {
                objectName: "chaptersBackButton"
                compact: true
                text: "Back"
                onClicked: Backend.goTo("drop")
            }

            PillButton {
                objectName: "chooseVoiceButton"
                Layout.fillWidth: true
                compact: true
                variant: "filled"
                text: "Next"
                Accessible.name: "Next: choose a voice"
                enabled: !page.nothingSelected
                onClicked: Backend.goTo("voice")
            }
        }
    }
}
