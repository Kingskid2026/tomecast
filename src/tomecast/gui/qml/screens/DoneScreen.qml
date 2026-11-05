// Done: the full grid lit, the length, the file and Open folder (Done.dc.html).
import QtQuick
import QtCore
import QtQuick.Layouts
import TomecastNative
import ".."
import "../components"

ScreenPage {
    id: page

    readonly property string homePath: {
        const home = StandardPaths.writableLocation(StandardPaths.HomeLocation).toString()
        return home.indexOf("file://") === 0 ? decodeURIComponent(home.substring(7)) : home
    }
    // The saved file, with the home folder written as "~".
    readonly property string shortPath: homePath !== "" && Backend.outputPath.indexOf(homePath + "/") === 0
                                        ? "~" + Backend.outputPath.substring(homePath.length)
                                        : Backend.outputPath
    readonly property string checkedCount: grouped(Backend.chunksTotal) + " OF " + grouped(Backend.chunksTotal)
    readonly property string coverDescription: "Cover of " + Backend.bookTitle + ", shown as dots"

    function grouped(n) {
        return Number(n).toLocaleString(Qt.locale("en_US"), "f", 0)
    }

    objectName: "doneScreen"
    designHeight: 720
    Accessible.role: Accessible.Pane
    Accessible.name: "Done"

    // Problems that didn't stop the book (a damaged cover left out, say).
    component Warnings: ColumnLayout {
        id: list
        objectName: "doneWarnings"
        visible: Backend.warnings.length > 0
        spacing: 6

        Repeater {
            model: Backend.warnings
            delegate: MonoLabel {
                required property string modelData
                Layout.fillWidth: true
                text: modelData.toUpperCase()
                size: 11
                wrapMode: Text.Wrap
            }
        }
    }


    Header {
        Layout.fillWidth: true
        title: "Done"

        MonoLabel {
            objectName: "checkedLabel"
            width: Math.min(implicitWidth, page.innerWidth)
            wrapMode: Text.Wrap
            text: "EVERY SENTENCE CHECKED · " + page.checkedCount
        }
    }

    GridLayout {
        id: body
        readonly property bool sideBySide: page.innerWidth >= 200 + Theme.columnGap + 560

        Layout.fillWidth: true
        columns: sideBySide ? 2 : 1
        columnSpacing: Theme.columnGap
        rowSpacing: Theme.columnGap

        DotImage {
            objectName: "doneCover"
            Layout.alignment: Qt.AlignTop
            visible: Backend.coverSource !== ""
            source: Backend.coverSource
            displayWidth: 200
            placeholderHeight: 276
            description: page.coverDescription
        }

        ColumnLayout {
            id: main
            Layout.alignment: Qt.AlignTop
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            spacing: 22

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 8

                MonoLabel {
                    objectName: "finishedLabel"
                    Layout.fillWidth: true
                    text: "FINISHED · " + Backend.bookTitle.toUpperCase()
                    size: 12
                    wrapMode: Text.Wrap
                }

                DisplayNumber {
                    objectName: "audioLength"
                    text: Backend.audioLength
                    size: page.bigNumberSize
                    Accessible.name: Backend.audioLength + " " + Backend.doneSummary.toLowerCase()
                }

                MonoLabel {
                    objectName: "doneSummary"
                    Layout.fillWidth: true
                    text: Backend.doneSummary
                    size: 12
                    tracking: 0.1
                    wrapMode: Text.Wrap
                }
            }

            SentenceGrid {
                objectName: "completeGrid"
                availableWidth: main.width
                total: Backend.chunksTotal
                done: Backend.chunksTotal
                complete: true
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 6

                MonoLabel {
                    text: "SAVED AS"
                    tracking: 0.14
                }

                Text {
                    objectName: "outputPath"
                    Layout.fillWidth: true
                    text: page.shortPath
                    color: Theme.text
                    font.family: Theme.mono
                    font.pixelSize: 15
                    wrapMode: Text.WrapAnywhere
                    textFormat: Text.PlainText
                }
            }

            Warnings {
                Layout.fillWidth: true
            }

            Flow {
                Layout.fillWidth: true
                spacing: 10

                PillButton {
                    objectName: "openFolderButton"
                    variant: "filled"
                    text: "Open folder"
                    onClicked: Backend.openOutputFolder()
                }

                PillButton {
                    objectName: "convertAnotherButton"
                    text: "Convert another book"
                    onClicked: Backend.convertAnother()
                }
            }
        }
    }

    // Spare height stays at the bottom.
    Item { Layout.fillHeight: true; Layout.topMargin: -page.sectionGap }
}
