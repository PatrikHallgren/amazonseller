import QtQuick
import QtQuick.Controls as Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
    id: root
    moduleName: "patrik.amazon-seller"
    ipcTarget: moduleName
    implicitWidth: barButton.implicitWidth
    implicitHeight: barButton.implicitHeight

    property var dashboard: ({products: [], alerts: [], shipments: [], periods: {}, sources: {}, settings: {period: "week"}, stock: {}})
    property bool expanded: false
    property string page: "Products"
    property string query: ""
    property string detailSku: ""
    property bool manageProducts: false
    property string notice: ""
    property var commands: []
    readonly property string executable: Qt.resolvedUrl("amazon-seller").toString().replace(/^file:\/\//, "")
    readonly property string dataPath: (Quickshell.env("XDG_DATA_HOME") || Quickshell.env("HOME") + "/.local/share") + "/amazon-seller/snapshot.json"
    readonly property string period: (dashboard.settings.period === "day" || dashboard.settings.period === "today") ? "week" : (dashboard.settings.period || "week")
    readonly property string metric: (dashboard.settings.metric === "orders" || dashboard.settings.metric === "returns") ? "units" : (dashboard.settings.metric || "units")
    readonly property string metricLabel: ({units:"Units sold", revenue:"Sales"})[metric] || "Units sold"
    readonly property var currentPeriod: dashboard.periods[period] || {current: {}, previous: {}, dates: [], span: []}
    readonly property var current: currentPeriod.current
    readonly property var salesSource: dashboard.sources.sales || dashboard.sources.orders || ({})
    readonly property bool hasSales: !!(salesSource && salesSource.success && salesSource.state !== "unavailable")
    readonly property var sourceNames: Object.keys(dashboard.sources || {}).filter(function(name) {
        var source = dashboard.sources[name] || {}
        return !(name === "returns" && source.state === "unavailable")
    })
    readonly property var selectedProduct: dashboard.products.find(function(p) { return p.sku === root.detailSku }) || null
    readonly property var visibleProducts: dashboard.products.filter(function(p) {
        return (root.manageProducts || p.selected) && (p.name + " " + p.sku + " " + p.asin).toLowerCase().indexOf(root.query.toLowerCase()) >= 0
    })
    readonly property int attentionCount: dashboard.alerts.filter(function(a) { return !a.acknowledged }).length
    readonly property string headline: metricValue(metric)

    function run(args) {
        commands = commands.concat([args])
        nextCommand()
    }
    function nextCommand() {
        if (command.running || commands.length === 0) return
        command.command = ["python3", executable].concat(commands[0])
        commands = commands.slice(1)
        command.running = true
    }
    function change(values) { run(["settings", JSON.stringify(values)]) }
    function cycle(direction) {
        var periods = ["week", "month", "year"]
        var index = periods.indexOf(period)
        if (index < 0) index = 0
        change({period: periods[(index + direction + periods.length) % periods.length]})
    }
    function selectProduct(sku) {
        var values = dashboard.products.filter(function(p) { return p.selected }).map(function(p) { return p.sku })
        var index = values.indexOf(sku)
        if (index < 0) values.push(sku); else values.splice(index, 1)
        change({selected: values})
    }
    function pinProduct(sku) {
        var values = (dashboard.settings.pinned || []).slice()
        var index = values.indexOf(sku)
        if (index < 0) values.push(sku); else values.splice(index, 1)
        change({pinned: values})
    }
    function metricValue(key) {
        if (!hasSales || current[key] === undefined || current[key] === null) return "—"
        var complete = current.sales_complete && (key !== "revenue" || current.revenue_complete)
        return ((complete && !current.provisional) ? "" : "~") + (key === "revenue" ? dollars(current[key]) : number(current[key]))
    }
    function number(value) { return value === null || value === undefined ? "—" : String(value) }
    function dollars(value) { return value === null || value === undefined ? "—" : "$" + Number(value).toLocaleString(Qt.locale("en_US"), "f", 2) }
    function date(value) { return value ? new Date(value).toLocaleString(Qt.locale("en_US"), "MMM d, HH:mm") : "Not available" }
    function age(value) {
        if (!value) return "Not synced"
        var minutes = Math.max(0, Math.floor((Date.now()-Date.parse(value))/60000))
        if (minutes < 1) return "Just updated"
        if (minutes < 60) return minutes + "m ago"
        var hours = Math.floor(minutes/60)
        if (hours < 48) return hours + "h ago"
        return Math.floor(hours/24) + "d ago"
    }
    function stockLine(p) {
        var inbound = (p.inbound_shipped || 0) + (p.inbound_receiving || 0)
        var transfer = p.fc_transfer ? " · " + number(p.fc_transfer) + " transferring" : ""
        var third = p.warehouse_units === null || p.warehouse_units === undefined ? "3PL —" : "3PL " + number(p.warehouse_units) + " (" + number(p.warehouse_cases) + " cases)"
        var total = p.total_us === null || p.total_us === undefined ? "US —" : "US " + number(p.total_us)
        var weeks = p.weeks_cover === null || p.weeks_cover === undefined ? "" : " · " + p.weeks_cover + " wk cover"
        var ship = p.suggested_cases > 0 ? " · send " + p.suggested_cases + " cases" : ""
        var days = p.days_left === null || p.days_left === undefined ? (p.forecast_note || "") : p.days_left + " days left"
        return number(p.available) + " FBA" + transfer + " / " + inbound + " incoming · " + third + " · " + total + weeks + ship + " · " + days
    }
    function stockSummary() {
        var s = dashboard.stock || {}
        if (s.total_us === null || s.total_us === undefined) return ""
        var weeks = s.weeks_cover === null || s.weeks_cover === undefined ? "cover unknown" : s.weeks_cover + " weeks of cover"
        var cases = s.warehouse_cases === null || s.warehouse_cases === undefined ? "" : " (" + s.warehouse_cases + " cases)"
        var partial = s.warehouse_partial ? " · 3PL not linked for every product" : ""
        return "Selected US stock " + number(s.total_us) + " · " + weeks + " · FBA " + number(s.fba_available) + " · 3PL " + number(s.warehouse_units) + cases + partial
    }
    function freshness() {
        var salesAt = salesSource.success || ""
        var stockAt = dashboard.sources.inventory ? dashboard.sources.inventory.success : ""
        var houseAt = dashboard.sources.warehouse ? dashboard.sources.warehouse.success : ""
        var err = dashboard.source_error ? dashboard.source_error + " · " : ""
        var stale = dashboard.stale ? "◌ Stale · " : ""
        return stale + err + "Snapshot " + age(dashboard.data_generated_at) + " · Sales " + age(salesAt) + " · FBA " + age(stockAt) + " · 3PL " + age(houseAt)
    }
    function dayLabel(value) {
        if (!value) return "Not available"
        var parts = String(value).split("-")
        if (parts.length < 3) return String(value)
        var parsed = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]))
        return parsed.toLocaleString(Qt.locale("en_US"), "MMM d")
    }
    function spanText() {
        var span = currentPeriod.span || []
        if (span.length < 4) return ""
        return dayLabel(span[0]) + "–" + dayLabel(span[1]) + " vs " + dayLabel(span[2]) + "–" + dayLabel(span[3])
    }
    function delta(metric) {
        if (currentPeriod.note) return currentPeriod.note
        var currentRow = currentPeriod.current || {}
        var previousRow = currentPeriod.previous || {}
        var a = currentRow[metric], b = previousRow[metric]
        var currentComplete = currentRow.sales_complete && (metric !== "revenue" || currentRow.revenue_complete)
        var previousComplete = previousRow.sales_complete && (metric !== "revenue" || previousRow.revenue_complete)
        if (a === undefined || a === null || b === undefined || b === null) return "Awaiting data"
        if (!currentComplete || !previousComplete) return "Comparison incomplete"
        if (!b) return a ? "No previous sales" : "No change"
        var pct = ((a-b)*100/b).toFixed(1)
        return (a-b > 0 ? "+" : "") + (metric === "revenue" ? dollars(a-b) : String(a-b)) + " · " + (pct > 0 ? "+" : "") + pct + "%"
    }
    function openUrl(url) { if (url.indexOf("https://sellercentral.amazon.com/") === 0) Qt.openUrlExternally(url) }

    FileView {
        id: snapshotFile
        path: root.dataPath
        watchChanges: true
        printErrors: false
        onLoaded: {
            try { root.dashboard = JSON.parse(text()); root.notice = "" }
            catch (e) { root.notice = "Waiting for a complete dashboard update" }
        }
        onFileChanged: reload()
        onLoadFailed: root.notice = "Start the Amazon Seller service or run setup"
    }
    Timer { interval: 15000; running: true; repeat: true; onTriggered: snapshotFile.reload() }
    Process {
        id: command
        stderr: StdioCollector { onStreamFinished: if (text.trim()) root.notice = text.trim() }
        stdout: StdioCollector {}
        onExited: function(code, status) { snapshotFile.reload(); Qt.callLater(root.nextCommand) }
    }
    IpcHandler {
        target: "patrik.amazon-seller.view"
        function inspect(): string { return JSON.stringify({bar:!!root.bar, width:root.width, height:root.height, buttonWidth:barButton.width, buttonHeight:barButton.height, label:barButton.text, opened:root.opened, page:root.page, detail:root.detailSku}) }
        function showPage(name: string): void { if (["Products","Shipments","Attention","Settings"].indexOf(name) >= 0) { root.page = name; root.detailSku = ""; root.open() } }
        function showProduct(sku: string): void { root.detailSku = sku; root.open() }
        function setExpanded(value: bool): void { root.expanded = value }
    }

    component Txt: Text {
        color: Color.popups.text
        font.family: Style.font.family
        font.pixelSize: Style.font.body
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
    }
    component Small: Txt { color: Color.lock.placeholder; font.pixelSize: Style.font.body * 0.87 }
    component Chip: Button {
        focusable: true
        bordered: true
        foreground: Color.popups.text
        tooltipBackground: Color.tooltip.background
        tooltipForeground: Color.tooltip.text
        tooltipBorder: Color.tooltip.border
    }
    component Hint: Controls.ToolTip {
        id: hint
        delay: 0
        padding: Style.space(8)
        background: Rectangle {
            color: Color.tooltip.background
            radius: Style.cornerRadius
            border.width: 1
            border.color: Color.tooltip.border
        }
        contentItem: Text {
            text: hint.text
            color: Color.tooltip.text
            font.family: Style.font.family
            font.pixelSize: Style.font.body * 0.87
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
        }
    }
    component Field: Controls.TextField {
        color: Color.popups.text
        selectedTextColor: Color.popups.text
        selectionColor: Color.accent
        placeholderTextColor: Color.lock.placeholder
        font.family: Style.font.family
        font.pixelSize: Style.font.body
        padding: Style.space(8)
        selectByMouse: true
        background: Rectangle { color: "transparent"; radius: Style.cornerRadius; border.color: parent.activeFocus ? Color.accent : Color.popups.border; border.width: 1 }
    }
    component Metric: Rectangle {
        id: metricCard
        property string metricKey: ""
        readonly property bool selected: metricKey !== "" && root.metric === metricKey
        activeFocusOnTab: metricKey !== ""
        border.width: selected || activeFocus ? 2 : 0
        border.color: Color.accent
        Accessible.role: metricKey ? Accessible.Button : Accessible.StaticText
        Accessible.name: label + ": " + value + (selected ? ", selected for bar" : "")
        function choose() { if (metricKey) root.change({metric: metricKey}) }
        Keys.onReturnPressed: choose()
        Keys.onEnterPressed: choose()
        Keys.onSpacePressed: choose()
        property string label: ""
        property string value: ""
        property string comparison: ""
        width: (stats.width - Style.space(8) * (stats.columns-1)) / stats.columns
        height: Style.space(88)
        radius: Style.cornerRadius
        color: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.07)
        MouseArea {
            id: metricMouse
            anchors.fill: parent
            enabled: metricCard.metricKey !== ""
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: { metricCard.forceActiveFocus(); metricCard.choose() }
        }
        Hint { visible: metricMouse.containsMouse; text: "Show " + label.toLowerCase() + " in the bar for the selected time frame" }
        Column {
            anchors.fill: parent; anchors.margins: Style.space(10); spacing: Style.space(3)
            Small { text: label }
            Txt { text: value; font.pixelSize: Style.font.body * 1.7; font.bold: true }
            Small { width: parent.width; text: comparison; maximumLineCount: 2; elide: Text.ElideRight }
        }
    }

    WidgetButton {
        id: barButton
        bar: root.bar
        foreground: root.bar ? root.bar.barForeground : Color.bar.text
        activeColor: root.bar ? root.bar.urgent : Color.bar.active
        text: "\uf290 " + root.headline + " · " + root.period.charAt(0).toUpperCase() + (root.attentionCount ? "  !" + root.attentionCount : "") + (root.dashboard.stale ? " ◌" : "") + (root.dashboard.mode === "demo" ? " DEMO" : "")
        active: root.attentionCount > 0
        tooltipText: "Amazon Seller" + (root.dashboard.mode === "demo" ? " · DEMO DATA" : "") + (root.dashboard.stale ? " · STALE" : "") + "\n" + root.headline + " " + root.metricLabel.toLowerCase() + " · " + root.period + "\n" + root.delta(root.metric) + "\n" + (root.dashboard.selected_count || 0) + " selected products · " + root.age(root.dashboard.data_generated_at || root.salesSource.success)
        onPressed: function(button) { root.toggle() }
        onWheelMoved: function(delta) { root.cycle(delta > 0 ? 1 : -1) }
    }

    KeyboardPanel {
        id: popup
        anchorItem: barButton
        owner: root
        bar: root.bar
        open: root.opened
        focusTarget: content
        contentWidth: fittedContentWidth(Style.space(root.expanded ? 860 : 530))
        contentHeight: fittedContentHeight(Style.space(root.expanded ? 710 : 620))

        Column {
            id: content
            anchors.fill: parent
            spacing: Style.space(10)
            focus: true
            Keys.onEscapePressed: { if (root.detailSku) root.detailSku = ""; else root.close() }
            Keys.onPressed: function(event) {
                if (event.key === Qt.Key_R && event.modifiers === Qt.ControlModifier) { root.run(["refresh"]); event.accepted = true }
            }
            Row {
                width: parent.width; spacing: Style.space(8)
                Column {
                    width: parent.width - expandButton.width - refreshButton.width - Style.space(16)
                    Txt { text: "Amazon Seller"; font.bold: true; font.pixelSize: Style.font.body * 1.4 }
                    Small { text: (root.dashboard.mode === "demo" ? "DEMO · Synthetic products and sales" : "US · FBA + Sumner 3PL · USD · Pacific time") + (root.dashboard.stale ? " · STALE" : "") }
                }
                Chip { id: refreshButton; text: "↻"; tooltipText: "Refresh · Ctrl+R"; onClicked: root.run(["refresh"]) }
                Chip { id: expandButton; text: root.expanded ? "Compact" : "Expand"; onClicked: root.expanded = !root.expanded }
            }
            Flow {
                width: parent.width; spacing: Style.space(6)
                Repeater {
                    model: ["week", "month", "year"]
                    Chip { required property string modelData; text: ({week:"Weekly", month:"Monthly", year:"Yearly"})[modelData]; selected: root.period === modelData; onClicked: root.change({period: modelData}) }
                }
                Small { text: (root.dashboard.selected_count || 0) + " products"; topPadding: Style.space(8) }
            }
            Small { text: "Bar display: " + root.metricLabel + " · choose a metric below"; width: parent.width }
            Small {
                visible: !!root.dashboard.stale
                width: parent.width
                color: Color.urgent
                text: "◌ Stale data · snapshot is " + root.age(root.dashboard.data_generated_at) + ". Daily sales should be under " + (root.dashboard.settings.stale_hours || 36) + " hours old."
            }
            Grid {
                id: stats
                width: parent.width; columns: 2; spacing: Style.space(8)
                Metric { metricKey: "units"; label: "Units sold"; value: root.metricValue("units"); comparison: "Previous " + root.number(root.currentPeriod.previous.units) + " · " + root.delta("units") }
                Metric { metricKey: "revenue"; label: "Sales"; value: root.metricValue("revenue"); comparison: "Previous " + root.dollars(root.currentPeriod.previous.revenue) + " · " + root.delta("revenue") }
            }
            Small { width: parent.width; visible: root.stockSummary() !== ""; text: root.stockSummary() }
            Small {
                width: parent.width
                text: root.spanText() ? root.spanText() + (root.currentPeriod.note ? "\n" + root.currentPeriod.note : "") + "\n~ = the newest day may still change" + (root.current.scope === "combined" ? "\nEarlier days are combined, all products. Recent days are per product." : root.current.scope === "account" ? "\nCombined totals, all products." : "") : "Connect a snapshot or enable demo in Settings."
            }
            Flow {
                width: parent.width; spacing: Style.space(5)
                Repeater {
                    model: ["Products", "Shipments", "Attention", "Settings"]
                    Chip { required property string modelData; text: modelData + (modelData === "Attention" && root.attentionCount ? " · " + root.attentionCount : ""); selected: root.page === modelData && !root.detailSku; onClicked: { root.page = modelData; root.detailSku = "" } }
                }
            }
            Loader {
                width: parent.width
                height: Math.max(Style.space(120), content.height - y - footer.implicitHeight - Style.space(12))
                sourceComponent: root.detailSku && root.selectedProduct ? detailPage : root.page === "Products" ? productsPage : root.page === "Shipments" ? shipmentsPage : root.page === "Attention" ? attentionPage : settingsPage
            }
            Small {
                id: footer
                width: parent.width
                text: root.notice || root.freshness()
                maximumLineCount: 2; elide: Text.ElideRight
            }
        }
    }

    Component {
        id: productsPage
        Column {
            spacing: Style.space(7)
            Row {
                width: parent.width; spacing: Style.space(6)
                Field { width: parent.width - manage.width - Style.space(6); placeholderText: "Search name, SKU, ASIN"; text: root.query; onTextEdited: root.query = text }
                Chip { id: manage; text: root.manageProducts ? "Done" : "Select products"; onClicked: root.manageProducts = !root.manageProducts }
            }
            Row {
                visible: root.manageProducts; spacing: Style.space(6)
                Chip { text: "Select all"; onClicked: root.change({selected: root.dashboard.products.filter(function(p) { return p.fba }).map(function(p) { return p.sku })}) }
                Chip { text: "Clear"; onClicked: root.change({selected: []}) }
            }
            Small { width: parent.width; text: "FBA / incoming · 3PL units and cases · US stock and weeks of cover · cases to send" }
            ListView {
                width: parent.width; height: Math.max(80, parent.height-y); clip: true
                model: root.visibleProducts
                spacing: Style.space(6)
                Controls.ScrollBar.vertical: Controls.ScrollBar {}
                delegate: Rectangle {
                    required property var modelData
                    width: ListView.view.width; height: Style.space(116)
                    radius: Style.cornerRadius; color: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.05)
                    Column {
                        anchors.fill: parent; anchors.margins: Style.space(8); spacing: Style.space(3)
                        Row {
                            width: parent.width; spacing: Style.space(5)
                            Chip { text: modelData.pinned ? "★" : "☆"; tooltipText: "Pin product"; onClicked: root.pinProduct(modelData.sku) }
                            Chip { visible: root.manageProducts; text: modelData.selected ? "✓" : "+"; tooltipText: "Show/hide product"; onClicked: root.selectProduct(modelData.sku) }
                            Txt { width: Math.max(60, parent.width - (root.manageProducts ? Style.space(180) : Style.space(130))); text: modelData.name; maximumLineCount: 1; elide: Text.ElideRight; font.bold: true; topPadding: Style.space(5) }
                            Chip { text: "View"; onClicked: root.detailSku = modelData.sku }
                        }
                        Txt { width: parent.width; text: root.stockLine(modelData); maximumLineCount: 2; elide: Text.ElideRight }
                        Small { width: parent.width; text: modelData.sku + (modelData.stranded ? " · Stranded" : "") + (modelData.excess ? " · " + modelData.excess + " excess units" : "") + (!modelData.selected ? " · Hidden from totals" : ""); maximumLineCount: 1; elide: Text.ElideRight }
                    }
                }
                Small { anchors.centerIn: parent; visible: parent.count === 0; text: root.dashboard.products.length ? "No matching products" : "Products appear after the first snapshot" }
            }
        }
    }
    Component {
        id: shipmentsPage
        ListView {
            clip: true; spacing: Style.space(8); model: root.dashboard.shipments
            Controls.ScrollBar.vertical: Controls.ScrollBar {}
            delegate: Column {
                required property var modelData
                width: ListView.view.width; spacing: Style.space(5)
                Txt { width: parent.width; text: modelData.name; font.bold: true }
                Small { width: parent.width; text: modelData.status
                        + (modelData.units_expected === null || modelData.units_expected === undefined ? "" : " · " + modelData.units_expected + " expected")
                        + (modelData.units_located === null || modelData.units_located === undefined ? "" : " · " + modelData.units_located + " located")
                        + (modelData.destination ? " · " + modelData.destination : "")
                        + (modelData.delivery_window ? " · " + modelData.delivery_window : "")
                        + " · Arrival: " + root.date(modelData.eta) }
                Repeater {
                    model: modelData.items
                    Small { required property var modelData; width: parent.width; text: modelData.sku + " · " + root.number(modelData.shipped) + " shipped · " + root.number(modelData.received) + " received · " + root.number(modelData.remaining) + " remaining" }
                }
                Chip { text: "Open shipment ↗"; onClicked: root.openUrl(modelData.url) }
                Rectangle { width: parent.width; height: 1; color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.22) }
            }
            Small { anchors.centerIn: parent; visible: parent.count === 0; text: "No shipments available for selected products" }
        }
    }
    Component {
        id: attentionPage
        ListView {
            id: attentionList
            clip: true; spacing: Style.space(10); model: root.dashboard.alerts
            Controls.ScrollBar.vertical: Controls.ScrollBar {}
            header: Small {
                width: attentionList.width
                visible: !!(root.dashboard.buyer_messages && root.dashboard.buyer_messages.count)
                height: visible ? implicitHeight : 0
                text: root.dashboard.buyer_messages ? (root.dashboard.buyer_messages.count + " buyer messages waiting · oldest " + root.number(root.dashboard.buyer_messages.oldest_hours) + "h · reply within 24 hours") : ""
                bottomPadding: Style.space(6)
            }
            delegate: Column {
                required property var modelData
                width: ListView.view.width; spacing: Style.space(4)
                Txt { width: parent.width; text: (modelData.severity === "urgent" ? "! Urgent · " : "Attention · ") + modelData.title; color: modelData.severity === "urgent" ? Color.urgent : Color.popups.text; font.bold: true }
                Small { width: parent.width; text: modelData.detail }
                Small { width: parent.width; text: "Detected " + root.date(modelData.first_seen) + (modelData.acknowledged ? " · Acknowledged, still unresolved" : "") + (modelData.snoozed_until ? " · Snoozed until " + root.date(modelData.snoozed_until) : "") }
                Flow {
                    width: parent.width; spacing: Style.space(5)
                    Chip { text: "Open Seller Central ↗"; onClicked: root.openUrl(modelData.url) }
                    Chip { text: "Acknowledge"; enabled: !modelData.acknowledged; onClicked: root.run(["alert", "ack", modelData.id]) }
                    Chip { text: "Snooze 24h"; onClicked: root.run(["alert", "snooze", modelData.id]) }
                }
                Rectangle { width: parent.width; height: 1; color: Qt.rgba(Color.popups.text.r, Color.popups.text.g, Color.popups.text.b, 0.22) }
            }
            Small { anchors.centerIn: parent; visible: parent.count === 0 && !(root.dashboard.buyer_messages && root.dashboard.buyer_messages.count); width: parent.width; horizontalAlignment: Text.AlignHCenter; text: "No detected issues for selected products." }
        }
    }
    Component {
        id: detailPage
        Controls.ScrollView {
            clip: true; contentWidth: availableWidth
            Column {
                width: parent.width; spacing: Style.space(10)
                Chip { text: "← Products"; onClicked: root.detailSku = "" }
                Txt { width: parent.width; text: root.selectedProduct.name; font.bold: true }
                Small { text: root.selectedProduct.sku + " · " + root.selectedProduct.asin }
                Txt { width: parent.width; text: root.number(root.selectedProduct.available) + " FBA sellable · " + root.number(root.selectedProduct.reserved) + " reserved · " + root.number(root.selectedProduct.fc_transfer) + " FC transfer · " + root.number(root.selectedProduct.unsellable) + " unsellable" }
                Small { width: parent.width; text: "Inbound: " + root.number(root.selectedProduct.inbound_working) + " preparing · " + root.number(root.selectedProduct.inbound_shipped) + " shipped · " + root.number(root.selectedProduct.inbound_receiving) + " receiving" }
                Txt { width: parent.width; text: "3PL on hand · " + root.number(root.selectedProduct.warehouse_units) + " units · " + root.number(root.selectedProduct.warehouse_cases) + " cases" + (root.selectedProduct.warehouse_sku ? " · " + root.selectedProduct.warehouse_sku : "") }
                Txt { width: parent.width; text: "Total US stock · " + root.number(root.selectedProduct.total_us) + " units · " + (root.selectedProduct.weeks_cover === null || root.selectedProduct.weeks_cover === undefined ? "cover unknown" : root.selectedProduct.weeks_cover + " weeks of cover") }
                Small { width: parent.width; text: "US stock is FBA sellable, plus units transferring between fulfillment centers, plus inbound shipped and receiving, plus Smart Warehousing (Sumner) on hand. Preparing shipments stay in the 3PL count until Amazon marks them shipped. Reserved and unfulfillable units are separate." }
                Txt { width: parent.width; text: root.selectedProduct.restock_note || "" }
                Txt { width: parent.width; text: root.selectedProduct.factory_note || "" }
                Txt { width: parent.width; text: "Daily demand " + root.number(root.selectedProduct.daily_demand) + " · " + root.number(root.selectedProduct.days_left) + " days left" }
                Small { width: parent.width; text: root.selectedProduct.forecast_note + " · " + root.selectedProduct.history_days + " complete days of history" }
                Txt { width: parent.width; text: "FBA target " + root.number(root.selectedProduct.target) + " units · Raw estimate " + root.number(root.selectedProduct.suggested) }
                Small { width: parent.width; text: "Target = daily demand × (lead + safety + interval). The raw estimate subtracts FBA sellable and shipped/receiving inventory. The case quantity above is that estimate rounded to whole cases and capped by Sumner on-hand stock." }
                Txt { text: "Last 14 complete days"; font.bold: true }
                Row {
                    width: parent.width; height: Style.space(55); spacing: Style.space(3)
                    Repeater {
                        model: root.selectedProduct.trend_days || []
                        Item {
                            id: trendDay
                            required property var modelData
                            width: (parent.width-Style.space(3)*13)/14
                            height: parent.height
                            activeFocusOnTab: true
                            readonly property string description: modelData.date + " · " + (modelData.complete ? "" : "~") + modelData.units + " units sold" + (modelData.complete ? "" : " · Incomplete history")
                            Accessible.role: Accessible.StaticText
                            Accessible.name: description
                            Rectangle {
                                width: parent.width
                                height: Math.max(2, parent.height*trendDay.modelData.units/Math.max.apply(null, root.selectedProduct.trend.concat([1])))
                                anchors.bottom: parent.bottom
                                color: Color.accent
                                opacity: trendHover.containsMouse || trendDay.activeFocus ? 1 : 0.7
                            }
                            MouseArea { id: trendHover; anchors.fill: parent; hoverEnabled: true; onClicked: trendDay.forceActiveFocus() }
                            Hint { visible: trendHover.containsMouse || trendDay.activeFocus; text: trendDay.description }
                        }
                    }
                }
                Small { text: root.selectedProduct.trend_complete ? "Units sold, oldest → newest · hover for date and count" : "Trend incomplete — missing history may understate sales" }
                Txt { width: parent.width; text: root.selectedProduct.amazon_recommendation ? "Amazon recommends " + root.number(root.selectedProduct.amazon_recommendation.quantity) + " units · ship " + (root.selectedProduct.amazon_recommendation.date || "date unavailable") : "Amazon recommendation unavailable" }
                Small { text: root.selectedProduct.amazon_recommendation ? "Updated " + root.date(root.selectedProduct.amazon_recommendation.updated) : "" }
                Txt { text: "Planning assumptions"; font.bold: true }
                PlanningFields { width: parent.width; sku: root.detailSku; values: root.selectedProduct.assumptions }
                Txt { text: "Return reasons · last 30 days"; font.bold: true }
                Repeater { model: root.selectedProduct.return_reasons; Small { required property var modelData; text: modelData.reason + " · " + modelData.units + " units" } }
                Chip { text: "Open product in Seller Central ↗"; onClicked: root.openUrl(root.selectedProduct.url) }
            }
        }
    }
    component PlanningFields: Column {
        id: planning
        property string sku: ""
        property var values: ({lead_days:30, safety_days:14, interval_days:30, reviewed:false})
        spacing: Style.space(6)
        Small { width: parent.width; text: values.reviewed ? "Your reviewed planning assumptions" : "Default estimates — review for your supply chain" }
        Row {
            spacing: Style.space(6)
            Column { Small { text: "Lead days" } Field { id: lead; width: Style.space(95); text: String(planning.values.lead_days); validator: IntValidator { bottom: 0; top: 730 } } }
            Column { Small { text: "Safety days" } Field { id: safety; width: Style.space(95); text: String(planning.values.safety_days); validator: IntValidator { bottom: 0; top: 730 } } }
            Column { Small { text: "Interval days" } Field { id: interval; width: Style.space(95); text: String(planning.values.interval_days); validator: IntValidator { bottom: 0; top: 730 } } }
        }
        Chip {
            text: "Save reviewed assumptions"
            enabled: lead.acceptableInput && safety.acceptableInput && interval.acceptableInput
            onClicked: {
                var value = {lead_days:Number(lead.text), safety_days:Number(safety.text), interval_days:Number(interval.text), reviewed:true}
                if (planning.sku) {
                    var overrides = JSON.parse(JSON.stringify(root.dashboard.settings.overrides || {}))
                    overrides[planning.sku] = value
                    root.change({overrides:overrides})
                } else root.change(value)
            }
        }
    }
    Component {
        id: settingsPage
        Controls.ScrollView {
            clip: true; contentWidth: availableWidth
            Column {
                width: parent.width; spacing: Style.space(10)
                Txt { text: "Connection"; font.bold: true }
                Small { width: parent.width; text: root.dashboard.mode === "demo" ? "Demo data is synthetic and stays separate from your snapshot." : root.dashboard.setup_required ? "Point the panel at a daily snapshot. No Amazon developer application is required." : "Snapshot source connected" }
                Flow {
                    width: parent.width; spacing: Style.space(6)
                    Chip { text: "Demo"; selected: root.dashboard.mode === "demo"; onClicked: root.run(["mode", "demo"]) }
                    Chip { text: "Live"; selected: root.dashboard.mode === "live"; onClicked: root.run(["mode", "live"]) }
                    Chip { text: "Connect snapshot…"; onClicked: Quickshell.execDetached(["omarchy", "launch", "terminal", "python3", root.executable, "setup"]) }
                }
                Chip { text: "Setup guide ↗"; onClicked: Qt.openUrlExternally(Qt.resolvedUrl("README.md")) }
                Small { width: parent.width; text: root.dashboard.settings.source === "file" ? "Local file · " + (root.dashboard.settings.snapshot_file || "") : root.dashboard.settings.source === "spapi" ? "Legacy SP-API" : "GitHub · " + (root.dashboard.settings.github_owner || "") + "/" + (root.dashboard.settings.github_repo || "") + " · " + (root.dashboard.settings.github_path || "") }
                Small { width: parent.width; text: "History: " + ((root.dashboard.history && root.dashboard.history.start) ? root.dashboard.history.start + " → " + (root.dashboard.history.end || "") + " · " + (root.dashboard.history.days || root.dashboard.history.complete || 0) + " sales days" : (root.dashboard.history ? (root.dashboard.history.complete || 0) + "/" + (root.dashboard.history.windows || 0) + " sales windows loaded" : "Not started")) }
                Repeater {
                    model: root.sourceNames
                    Small { required property string modelData; width: parent.width; text: modelData + ": " + root.dashboard.sources[modelData].state + " · " + (root.dashboard.sources[modelData].error || root.age(root.dashboard.sources[modelData].success)) }
                }
                Txt { text: "Notifications"; font.bold: true }
                Chip { text: root.dashboard.settings.notifications ? "Attention notifications: on" : "Attention notifications: off"; onClicked: root.change({notifications: !root.dashboard.settings.notifications}) }
                Small { width: parent.width; text: "Stock, shipments, and Seller Central action items in the snapshot. Buyer messages include the 24-hour reply target and turn urgent after about 20 hours. A section that is missing from the snapshot stays blank. Desktop Do Not Disturb is respected." }
                Chip { text: "Open customer inbox ↗"; onClicked: root.openUrl("https://sellercentral.amazon.com/messaging/inbox") }
                Row {
                    spacing: Style.space(8)
                    Column { Small { text: "Return minimum" } Field { id: returnMin; width: Style.space(105); text: String(root.dashboard.settings.return_min || 3); validator: IntValidator { bottom: 1; top: 730 } } }
                    Column { Small { text: "Prior-week multiplier" } Field { id: returnMultiple; width: Style.space(150); text: String(root.dashboard.settings.return_multiplier || 2); validator: IntValidator { bottom: 1; top: 730 } } }
                }
                Chip { text: "Save return threshold"; enabled: returnMin.acceptableInput && returnMultiple.acceptableInput; onClicked: root.change({return_min:Number(returnMin.text), return_multiplier:Number(returnMultiple.text)}) }
                Txt { text: "Factory reorder · Shenzhen"; font.bold: true }
                Small { width: parent.width; text: "Alert when total US stock covers fewer days of sales than this lead time. Default 180 days, about six months, for a reorder from the Shenzhen factory." }
                Row {
                    spacing: Style.space(8)
                    Column { Small { text: "Factory lead days" } Field { id: factoryLead; width: Style.space(140); text: String(root.dashboard.settings.factory_lead_days || 180); validator: IntValidator { bottom: 0; top: 730 } } }
                }
                Chip { text: "Save factory lead time"; enabled: factoryLead.acceptableInput; onClicked: root.change({factory_lead_days: Number(factoryLead.text)}) }
                Txt { text: "Default stock planning"; font.bold: true }
                PlanningFields { width: parent.width; values: root.dashboard.settings }
            }
        }
    }
}
