// A cover or artwork already rendered as dots by the "dots" image provider (no backdrop).
// The width is fixed; the height follows the image's own shape once it has loaded.
// sourceSize.width follows the displayed width, so the provider draws the dots at the exact
// size they're shown (it scales to the screen itself).
import QtQuick
import ".."

Item {
    id: root

    property alias source: image.source
    property real displayWidth: 240
    // Height to reserve until the image has loaded (the mockup's proportions).
    property real placeholderHeight: Math.round(displayWidth * 1.38)
    property string description: ""
    readonly property alias status: image.status
    // For tests and screenshots, which can't read the Image.Status enum from Python.
    readonly property bool imageLoading: image.status === Image.Loading

    implicitWidth: displayWidth
    implicitHeight: image.status === Image.Ready && image.implicitWidth > 0
                    ? Math.round(displayWidth * image.implicitHeight / image.implicitWidth)
                    : placeholderHeight

    Accessible.role: Accessible.Graphic
    Accessible.name: description

    Image {
        id: image
        anchors.fill: parent
        sourceSize.width: root.displayWidth
        fillMode: Image.PreserveAspectFit
        asynchronous: true
        smooth: true
        Accessible.ignored: true
    }
}
