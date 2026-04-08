// place_markers.jsx  v1
// Premiere Pro ExtendScript
// analysis_debug.json または style_mapping.json を読み取り、シーケンスマーカーを配置
//
// 使い方:
//   1. YouTube Sequence をアクティブにする
//   2. JSON ファイルを選択（analysis_debug.json or style_mapping.json）
//   3. マーカーが配置される
//
// マーカーカラー:
//   0=Green, 1=Red, 2=Purple, 3=Orange, 4=Yellow, 5=White, 6=Blue, 7=Cyan

var COLOR_MAP = {
    // analysis_debug.json の key_points/sfx/transition 用
    "key_point": 1,      // Red - ズーム
    "sfx": 3,            // Orange - 効果音
    "transition": 6,     // Blue - トランジション
    "insert": 2,         // Purple - インサート
    "highlight": 4,      // Yellow - ハイライト
    // style_mapping.json のカテゴリ用
    "強調": 1,           // Red
    "ネガティブ": 6,     // Blue
    "ポジティブ": 4,     // Yellow
    "ツッコミ": 3,       // Orange
    "通常": 0            // Green
};

function parseJSON(str) {
    try { return eval("(" + str + ")"); } catch (e) { return null; }
}

function readFile(path) {
    var f = new File(path);
    if (!f.exists) return null;
    f.open("r");
    f.encoding = "UTF-8";
    var content = f.read();
    f.close();
    return content;
}

function addSeqMarker(seq, timeSec, name, comment, colorIdx) {
    var t = new Time();
    t.seconds = timeSec;
    var markers = seq.markers;
    var marker = markers.createMarker(t.ticks);
    marker.name = name;
    marker.comments = comment;
    marker.setColorByIndex(colorIdx);
    return marker;
}

(function () {
    var seq = app.project.activeSequence;
    if (!seq) {
        alert("エラー: アクティブなシーケンスが見つかりません。");
        return;
    }

    var jsonFile = File.openDialog("JSON ファイルを選択 (analysis_debug.json or style_mapping.json)", "JSON:*.json", false);
    if (!jsonFile) return;

    var content = readFile(jsonFile.fsName);
    if (!content) {
        alert("エラー: ファイルを読み込めませんでした。");
        return;
    }

    var data = parseJSON(content);
    if (!data) {
        alert("エラー: JSON パースに失敗しました。");
        return;
    }

    var count = 0;
    var report = "=== マーカー配置 ===\n";

    // analysis_debug.json 形式 (オブジェクト with key_points, sfx_events, etc.)
    if (data.key_points || data.sfx_events || data.transition_events) {
        // key_points
        if (data.key_points) {
            for (var i = 0; i < data.key_points.length; i++) {
                var kp = data.key_points[i];
                addSeqMarker(seq, kp.at_ms / 1000, "ZOOM", kp.reason || "", COLOR_MAP["key_point"]);
                count++;
            }
            report += "key_points: " + data.key_points.length + " マーカー\n";
        }

        // sfx_events
        if (data.sfx_events) {
            for (var i = 0; i < data.sfx_events.length; i++) {
                var sfx = data.sfx_events[i];
                var label = "SFX:" + (sfx.sfx_id || "");
                addSeqMarker(seq, sfx.at_ms / 1000, label, sfx.reason || "", COLOR_MAP["sfx"]);
                count++;
            }
            report += "sfx_events: " + data.sfx_events.length + " マーカー\n";
        }

        // transition_events
        if (data.transition_events) {
            for (var i = 0; i < data.transition_events.length; i++) {
                var tr = data.transition_events[i];
                var label = "TR:" + (tr.type || "dissolve");
                addSeqMarker(seq, tr.at_ms / 1000, label, tr.reason || "", COLOR_MAP["transition"]);
                count++;
            }
            report += "transition_events: " + data.transition_events.length + " マーカー\n";
        }

        // insert_events
        if (data.insert_events) {
            for (var i = 0; i < data.insert_events.length; i++) {
                var ins = data.insert_events[i];
                var atMs = ins.start_ms || ins.at_ms || 0;
                addSeqMarker(seq, atMs / 1000, "INSERT", ins.prompt_en || "", COLOR_MAP["insert"]);
                count++;
            }
            report += "insert_events: " + data.insert_events.length + " マーカー\n";
        }

        // highlight
        if (data.highlight) {
            var hl = data.highlight;
            addSeqMarker(seq, hl.at_ms / 1000, "HIGHLIGHT", hl.reason || "", COLOR_MAP["highlight"]);
            count++;
            report += "highlight: 1 マーカー\n";
        }
    }
    // style_mapping.json 形式 (配列 with category)
    else if (data.length !== undefined && data.length > 0 && data[0].category) {
        for (var i = 0; i < data.length; i++) {
            var entry = data[i];
            var cat = entry.category || "通常";
            var colorIdx = COLOR_MAP[cat] !== undefined ? COLOR_MAP[cat] : 0;
            var timeSec = (entry.start_ms || 0) / 1000;
            addSeqMarker(seq, timeSec, cat, entry.text || "", colorIdx);
            count++;
        }
        report += "テロップマーカー: " + data.length + " 個\n";
    } else {
        alert("エラー: 対応するJSON形式ではありません。\n" +
              "analysis_debug.json または style_mapping.json を選択してください。");
        return;
    }

    report += "\n合計: " + count + " マーカー配置\n✅ 完了";
    alert(report);
})();
