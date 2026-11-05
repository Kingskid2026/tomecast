// [ 03 ] Voice: the voice menu grouped by accent and gender, and a preview with speed.
// Wide: Voice.dc.html. Compact (corner window): Corner-voice.dc.html, where the voice list
// scrolls by itself above the preview, the speed and the buttons.
import QtQuick
import QtQuick.Controls.Basic as Controls
import QtQuick.Layouts
import TomecastNative
import ".."
import "../components"

ScreenPage {
    id: page

    objectName: "voiceScreen"
    wideDesignHeight: 780
    Accessible.role: Accessible.Pane
    Accessible.name: "Voice"

    // The voices, grouped ("AMERICAN · FEMALE"), with the note about other languages.
    component VoiceMenu: ColumnLayout {
        id: menu
        property bool compact: false

        spacing: compact ? 14 : 22

        Repeater {
            model: Backend.voiceGroups
            delegate: ColumnLayout {
                id: group
                required property var modelData
                Layout.fillWidth: true
                spacing: menu.compact ? 8 : 10
                Accessible.role: Accessible.Grouping
                Accessible.name: modelData.label

                MonoLabel {
                    text: group.modelData.label
                    size: menu.compact ? 10 : 11
                    tracking: 0.14
                }

                Flow {
                    Layout.fillWidth: true
                    spacing: menu.compact ? 6 : 8

                    Repeater {
                        model: group.modelData.voices
                        delegate: ChoicePill {
                            required property var modelData
                            objectName: "voice_" + modelData.id
                            compact: menu.compact
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
            size: menu.compact ? 9 : 11
            wrapMode: Text.Wrap
        }
    }

    // The round play button with its dot waveform (or why there's no preview).
    component PreviewPlayer: RowLayout {
        id: player
        property bool compact: false
        // The waveform (decorative), from the mockup.
        readonly property var levels: [3, 5, 4, 7, 6, 8, 5, 3, 6, 8, 7, 4, 5, 6, 3, 2, 5, 7, 8, 6,
                                       4, 6, 5, 3, 4, 6, 7, 5, 3, 2, 4, 6, 5, 4, 3, 5, 4, 2, 3, 2]

        spacing: compact ? 14 : 18

        RoundButton {
            objectName: "previewButton"
            compact: player.compact
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
            dotSize: player.compact ? 4 : 5
            dotSpacing: player.compact ? 2 : 3
            spread: player.compact
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
            size: player.compact ? 10 : 11
            wrapMode: Text.Wrap
        }
    }

    // SPEED, the 11-dot meter from 0.75x to 1.25x, and its end labels.
    component SpeedControl: ColumnLayout {
        id: speed
        property bool compact: false
        readonly property int index: Math.round((Backend.speed - 0.75) / 0.05)

        function label(i) {
            return (0.75 + i * 0.05).toFixed(2) + "×"
        }

        spacing: compact ? 4 : 8

        RowLayout {
            Layout.fillWidth: true

            MonoLabel {
                Layout.fillWidth: true
                text: "SPEED"
                size: speed.compact ? 10 : 11
                tracking: 0.14
            }

            MonoLabel {
                objectName: "speedValue"
                text: speed.label(speed.index)
                size: speed.compact ? 10 : 11
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
            currentSize: 12
            cellWidth: speed.compact ? 30 : 28
            cellHeight: speed.compact ? 32 : Theme.controlHeight
            Accessible.name: "Speaking speed " + speed.label(speed.index)
            labelAt: function(i) { return "Speed " + speed.label(i) }
            onPicked: (i) => Backend.setSpeed(Math.round((0.75 + i * 0.05) * 100) / 100)
        }

        RowLayout {
            Layout.fillWidth: true

            MonoLabel {
                Layout.fillWidth: true
                text: "0.75×"
                size: speed.compact ? 9 : 10
                tracking: 0
            }

            MonoLabel {
                text: "1.25×"
                size: speed.compact ? 9 : 10
                tracking: 0
            }
        }
    }

    wideView: ColumnLayout {
        spacing: Theme.sectionGap

        Header {
            Layout.fillWidth: true
            index: 3
            title: "Voice"

            PillButton {
                objectName: "voiceBackButton"
                text: "Back"
                onClicked: Backend.goTo("chapters")
            }

            PillButton {
                objectName: "startConvertingButton"
                variant: "filled"
                text: "Start converting"
                onClicked: Backend.startConversion()
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
            index: 3
            title: "Voice"

            MonoLabel {
                objectName: "hardwareLabel"
                text: Backend.hardwareLabel.replace("ABOUT ", "")
                size: 10
            }
        }

        // The voices, scrolling by themselves.
        Flickable {
            id: voiceList
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 120
            Layout.preferredHeight: 120
            contentWidth: width
            contentHeight: menu.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            flickableDirection: Flickable.VerticalFlick
            Controls.ScrollBar.vertical: Controls.ScrollBar {
                contentItem: Rectangle {
                    implicitWidth: 3
                    radius: 1.5
                    color: Theme.muted
                    opacity: parent.active ? 0.8 : 0
                }
            }

            VoiceMenu {
                id: menu
                objectName: "voiceMenu"
                width: voiceList.width
                compact: true
            }
        }

        ColumnLayout {
            objectName: "previewPanel"
            Layout.fillWidth: true
            spacing: 8

            MonoLabel {
                objectName: "previewVoiceLabel"
                Layout.fillWidth: true
                text: "PREVIEW · " + Backend.voiceLabel
                size: 10
                tracking: 0.14
                elide: Text.ElideRight
            }

            PreviewPlayer {
                Layout.fillWidth: true
                compact: true
            }
        }

        SpeedControl {
            Layout.fillWidth: true
            compact: true
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            PillButton {
                objectName: "voiceBackButton"
                compact: true
                text: "Back"
                onClicked: Backend.goTo("chapters")
            }

            PillButton {
                objectName: "startConvertingButton"
                Layout.fillWidth: true
                compact: true
                leftPadding: 16
                rightPadding: 16
                variant: "filled"
                text: "Start converting"
                onClicked: Backend.startConversion()
            }
        }
    }
}
