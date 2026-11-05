// Drop a book: the Cantoria in dots, "DROP A BOOK HERE", and Choose a book (Main.dc.html).
// While a file is dragged over the window, "DROP A BOOK HERE" turns the accent colour.
import QtQuick
import QtQuick.Dialogs
import QtQuick.Layouts
import TomecastNative
import ".."
import "../components"

ScreenPage {
    id: page

    // A file is being dragged over the window.
    property bool dragActive: false
    readonly property string titleText: Backend.loading ? "READING THE BOOK…" : "DROP A BOOK HERE"
    readonly property color titleColor: dragActive ? Theme.accent : Theme.text
    readonly property string artDescription: "Luca della Robbia's Cantoria, marble children singing, shown as dots"

    function chooseBook() {
        // A file:// URL from the backend (a plain path also works).
        const folder = Backend.chooseBookFolder()
        if (folder !== "")
            fileDialog.currentFolder = folder.indexOf("file:") === 0 ? folder : "file://" + folder
        fileDialog.open()
    }

    objectName: "dropScreen"
    designHeight: 720
    // The mockup's page padding is 28 px top and bottom (24 px narrow).
    bottomPadding: narrow ? Theme.narrowPadBottom : Theme.pageTop
    sectionGap: narrow ? Theme.narrowGap : 24
    Accessible.role: Accessible.Pane
    Accessible.name: "Drop a book"

    Header {
        Layout.fillWidth: true

        MonoLabel {
            objectName: "hardwareLabel"
            text: Backend.hardwareLabel
        }
    }

    // The field: 40 px padding around a content box at least 520 px tall.
    Item {
        id: field
        objectName: "dropZone"
        readonly property real room: width - 2 * 40

        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.minimumHeight: Math.max(520, art.implicitHeight) + 2 * 40

        GridLayout {
            id: art
            // Art beside the words, or above them (centred) when they don't fit side by side.
            readonly property bool sideBySide: 240 + 56 + 420 <= field.room

            anchors.centerIn: parent
            width: sideBySide ? implicitWidth : Math.min(field.room, 420)
            columns: sideBySide ? 2 : 1
            columnSpacing: 56
            rowSpacing: 56

            DotImage {
                objectName: "artImage"
                Layout.alignment: art.sideBySide ? Qt.AlignVCenter : Qt.AlignHCenter
                source: Backend.artSource
                displayWidth: 240
                placeholderHeight: 358
                description: page.artDescription
            }

            ColumnLayout {
                Layout.alignment: Qt.AlignVCenter
                Layout.maximumWidth: 420
                Layout.preferredWidth: Math.min(420, field.room)
                spacing: 18

                MonoLabel {
                    objectName: "dropTitle"
                    Layout.fillWidth: true
                    text: page.titleText
                    size: 15
                    font.weight: Font.Medium
                    tracking: 0.18
                    color: page.titleColor
                    wrapMode: Text.Wrap

                    Behavior on color {
                        enabled: Theme.motion
                        ColorAnimation { duration: Theme.quickMs }
                    }
                }

                Text {
                    Layout.fillWidth: true
                    text: "Tomecast turns it into an audiobook with chapters, right on this computer."
                    color: Theme.muted
                    font.family: Theme.sans
                    font.pixelSize: 15
                    lineHeightMode: Text.FixedHeight
                    lineHeight: 24
                    wrapMode: Text.Wrap
                    textFormat: Text.PlainText
                }

                MonoLabel {
                    objectName: "formatsLabel"
                    Layout.fillWidth: true
                    text: Backend.formatsLabel
                    tracking: 0.14
                    wrapMode: Text.Wrap
                }

                PillButton {
                    objectName: "chooseBookButton"
                    variant: "strong"
                    tracking: 0.14
                    text: "Choose a book"
                    enabled: !Backend.loading
                    onClicked: page.chooseBook()
                }
            }
        }
    }

    SplitRow {
        Layout.fillWidth: true

        MonoLabel {
            objectName: "noBookLabel"
            Layout.fillWidth: true
            text: "NO BOOK YET"
        }

        MonoLabel {
            Layout.maximumWidth: page.innerWidth
            text: "KOKORO 82M · AUDIO.CPP 0.9.4 · EVERYTHING STAYS ON THIS COMPUTER"
            wrapMode: Text.Wrap
        }
    }

    FileDialog {
        id: fileDialog
        objectName: "bookFileDialog"
        title: "Choose a book"
        fileMode: FileDialog.OpenFile
        nameFilters: [
            "Books (*.epub *.docx *.fb2 *.html *.htm *.xhtml *.txt)",
            "All files (*)"
        ]
        onAccepted: Backend.openBook(selectedFile)
    }
}
