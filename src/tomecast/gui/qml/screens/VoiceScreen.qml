// Voice: the voice menu grouped by accent and gender, and a preview with speed (Voice.dc.html).
// In a narrow window the preview panel moves under the voice menu.
import QtQuick
import QtQuick.Layouts
import TomecastNative
import ".."
import "../components"

ScreenPage {
    id: page

    objectName: "voiceScreen"
    designHeight: 780
    Accessible.role: Accessible.Pane
    Accessible.name: "Voice"

    // The voices, grouped ("AMERICAN · FEMALE"), with the note about other languages.
    component VoiceMenu: ColumnLayout {
        id: menu

        spacing: 22

        Repeater {
            model: Backend.voiceGroups
            delegate: ColumnLayout {
                id: group
                required property var modelData
                Layout.fillWidth: true
                spacing: 10
                Accessible.role: Accessible.Grouping
                Accessible.name: modelData.label

                MonoLabel {
                    text: group.modelData.label
                    size: 11
                    tracking: 0.14
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: 8

                    Repeater {
                        model: group.modelData.voices
                        delegate: ChoicePill {
                            required property var modelData
                            objectName: "voice_" + modelData.id
                            text: modelData.name
                            chosen: Backend.voice === modelData.id
                            Accessible.name: modelData.name + ", "
                                             + group.modelData.label.toLowerCase().replace(" · ", " ")
                            onClicked: Backend.setVoice(modelData.id)
                        }
                    }
                }
            }
        }

        MonoLabel {
            Layout.fillWidth: true
            text: "ENGLISH VOICES SHOWN · 54 VOICES IN 9 LANGUAGES"
            size: 11
            wrapMode: Text.Wrap
        }
    }

    // The round play button with its dot waveform (or why there's no preview).
    component PreviewPlayer: RowLayout {
        id: player
        // The waveform (decorative), from the mockup.
        readonly property var levels: [3, 5, 4, 7, 6, 8, 5, 3, 6, 8, 7, 4, 5, 6, 3, 2, 5, 7, 8, 6,
                                       4, 6, 5, 3, 4, 6, 7, 5, 3, 2, 4, 6, 5, 4, 3, 5, 4, 2, 3, 2]

        spacing: 18

        RoundButton {
            objectName: "previewButton"
            glyph: Backend.previewPlaying ? "pause" : "play"
            enabled: Backend.previewAvailable
            text: !Backend.previewAvailable
                  ? "Preview unavailable: no audio player was found (pw-play, paplay or aplay)"
                  : Backend.previewPlaying ? "Pause preview" : "Play preview"
            onClicked: Backend.togglePreview()
        }

        DotMeter {
            objectName: "previewWave"
            visible: Backend.previewAvailable
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignVCenter
            count: player.levels.length
            dotSize: Theme.dotSize
            dotSpacing: Theme.dotGap
            singleLine: true
            colorAt: function(index) {
                return player.levels[index] > 5 ? (Backend.previewPlaying ? Theme.text : Theme.muted)
                                                : Theme.wait
            }
            Accessible.ignored: true
        }

        MonoLabel {
            objectName: "previewUnavailable"
            visible: !Backend.previewAvailable
            Layout.fillWidth: true
            text: "NO AUDIO PLAYER FOUND · PW-PLAY, PAPLAY OR APLAY"
            size: 11
            wrapMode: Text.Wrap
        }
    }

    // SPEED, the 11-dot meter over Backend.minSpeed..maxSpeed, and its end labels.
    component SpeedControl: ColumnLayout {
        id: speed
        readonly property int index: Math.round((Backend.speed - Backend.minSpeed) / Backend.speedStep)

        function valueAt(i) {
            return Math.round((Backend.minSpeed + i * Backend.speedStep) * 100) / 100
        }

        function label(i) {
            return valueAt(i).toFixed(2) + "×"
        }

        spacing: 8

        RowLayout {
            Layout.fillWidth: true

            MonoLabel {
                Layout.fillWidth: true
                text: "SPEED"
                size: 11
                tracking: 0.14
            }

            MonoLabel {
                objectName: "speedValue"
                text: speed.label(speed.index)
                size: 11
                tracking: 0.14
                color: Theme.text
            }
        }

        DotMeter {
            objectName: "speedMeter"
            Layout.fillWidth: true
            interactive: true
            count: 11
            current: speed.index
            cellWidth: 28
            cellHeight: Theme.controlHeight
            Accessible.name: "Speaking speed " + speed.label(speed.index)
            labelAt: function(i) { return "Speed " + speed.label(i) }
            onPicked: (i) => Backend.setSpeed(speed.valueAt(i))
        }

        RowLayout {
            Layout.fillWidth: true

            MonoLabel {
                Layout.fillWidth: true
                objectName: "speedMinLabel"
                text: Backend.minSpeed + "×"
                size: 10
                tracking: 0
            }

            MonoLabel {
                objectName: "speedMaxLabel"
                text: Backend.maxSpeed + "×"
                size: 10
                tracking: 0
            }
        }
    }

    Header {
        Layout.fillWidth: true
        title: "Voice"

        ColumnLayout {
            spacing: 10

            // What to make: one audiobook file, or a folder of MP3s (the same pills as the voices).
            Flow {
                objectName: "outputFormatChoice"
                Layout.alignment: Qt.AlignRight
                Layout.maximumWidth: page.innerWidth
                layoutDirection: Qt.RightToLeft
                spacing: 8
                Accessible.role: Accessible.Grouping
                Accessible.name: "Output format"

                ChoicePill {
                    objectName: "format_mp3"
                    text: "MP3 · ONE PER CHAPTER"
                    chosen: Backend.outputFormat === "mp3"
                    Accessible.name: "MP3, one file per chapter"
                    onClicked: Backend.setOutputFormat("mp3")
                }

                ChoicePill {
                    objectName: "format_m4b"
                    text: "AUDIOBOOK · M4B"
                    chosen: Backend.outputFormat === "m4b"
                    Accessible.name: "Audiobook, one M4B file"
                    onClicked: Backend.setOutputFormat("m4b")
                }
            }

            // What Start will download, if anything (the first time only).
            MonoLabel {
                objectName: "downloadNote"
                Layout.alignment: Qt.AlignRight
                Layout.fillWidth: true
                Layout.maximumWidth: page.innerWidth
                visible: Backend.downloadNote !== ""
                text: Backend.downloadNote
                size: 10
                tracking: 0.12
                horizontalAlignment: Text.AlignRight
                wrapMode: Text.Wrap
            }

            RowLayout {
                Layout.alignment: Qt.AlignRight
                spacing: 10

                PillButton {
                    objectName: "voiceBackButton"
                    text: "Back"
                    onClicked: Backend.goTo("chapters")
                }

                PillButton {
                    objectName: "startConvertingButton"
                    variant: "filled"
                    text: "Start converting"
                    enabled: !Backend.loading
                    onClicked: Backend.startConversion()
                }
            }
        }
    }

    GridLayout {
        id: body
        // The preview panel is 380 px plus 28 px padding on each side.
        readonly property bool sideBySide: page.innerWidth >= 520 + 48 + 436

        Layout.fillWidth: true
        columns: sideBySide ? 2 : 1
        columnSpacing: 48
        rowSpacing: 48

        VoiceMenu {
            objectName: "voiceMenu"
            Layout.alignment: Qt.AlignTop
            Layout.fillWidth: true
            Layout.minimumWidth: 0
        }

        // Preview and speed.
        Item {
            objectName: "previewPanel"
            Layout.alignment: Qt.AlignTop
            Layout.fillWidth: !body.sideBySide
            Layout.preferredWidth: 436
            Layout.maximumWidth: 516
            implicitHeight: preview.implicitHeight + 2 * 28

            ColumnLayout {
                id: preview
                x: 28
                y: 28
                width: parent.width - 2 * 28
                spacing: 24

                MonoLabel {
                    objectName: "previewVoiceLabel"
                    Layout.fillWidth: true
                    text: "PREVIEW · " + Backend.voiceLabel
                    tracking: 0.14
                    wrapMode: Text.Wrap
                }

                PreviewPlayer {
                    Layout.fillWidth: true
                }

                Text {
                    objectName: "previewText"
                    Layout.fillWidth: true
                    text: Backend.previewText
                    color: Theme.text
                    font.family: Theme.mono
                    font.pixelSize: 12
                    font.letterSpacing: 12 * 0.06
                    font.capitalization: Font.AllUppercase
                    lineHeightMode: Text.FixedHeight
                    lineHeight: 20.4
                    wrapMode: Text.Wrap
                    maximumLineCount: 4
                    elide: Text.ElideRight
                    textFormat: Text.PlainText
                }

                SpeedControl {
                    Layout.fillWidth: true
                }

                // Only when a graphics driver is there to use.
                ColumnLayout {
                    objectName: "gpuChoice"
                    Layout.fillWidth: true
                    visible: Backend.gpuAvailable
                    spacing: 6

                    CheckRow {
                        objectName: "useGpuSwitch"
                        text: "Use graphics card"
                        checked: Backend.useGpu
                        onClicked: Backend.setUseGpu(!Backend.useGpu)
                    }

                    MonoLabel {
                        objectName: "gpuNote"
                        Layout.fillWidth: true
                        text: Backend.useGpu
                              ? "FASTER · USES THE DRIVER ALREADY INSTALLED"
                              : "USING THE CPU · THE GRAPHICS CARD IS USUALLY SEVERAL TIMES FASTER"
                        size: 11
                        wrapMode: Text.Wrap
                    }
                }
            }
        }
    }

    // Spare height stays at the bottom.
    Item { Layout.fillHeight: true; Layout.topMargin: -page.sectionGap }
}
