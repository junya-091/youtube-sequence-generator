// export_markers.jsx  v1
// Premiere Pro ExtendScript
// アクティブシーケンスの全マーカーを JSON ファイルにエクスポート
//
// 使い方:
//   1. YouTube Sequence をアクティブにする
//   2. 実行 → 保存先を選択
//   3. markers_export.json が生成される

(function () {
    var seq = app.project.activeSequence;
    if (!seq) {
        alert("エラー: アクティブなシーケンスが見つかりません。");
        return;
    }

    var markers = seq.markers;
    if (markers.numMarkers === 0) {
        alert("シーケンスにマーカーがありません。");
        return;
    }

    var result = [];
    var marker = markers.getFirstMarker();
    var count = 0;

    while (marker) {
        var entry = {
            index: count + 1,
            name: marker.name || "",
            comments: marker.comments || "",
            start_seconds: marker.start.seconds,
            end_seconds: marker.end.seconds,
            duration_seconds: marker.end.seconds - marker.start.seconds,
            type: marker.type || "Comment"
        };

        // タイムコード表示用
        var sec = marker.start.seconds;
        var h = Math.floor(sec / 3600);
        var m = Math.floor((sec % 3600) / 60);
        var s = Math.floor(sec % 60);
        var f = Math.floor((sec % 1) * 30);
        entry.timecode = (h < 10 ? "0" : "") + h + ":" +
                         (m < 10 ? "0" : "") + m + ":" +
                         (s < 10 ? "0" : "") + s + ":" +
                         (f < 10 ? "0" : "") + f;

        result.push(entry);
        count++;
        marker = markers.getNextMarker(marker);
    }

    // JSON 文字列生成 (ExtendScript には JSON.stringify がないため手動)
    var jsonStr = "[\n";
    for (var i = 0; i < result.length; i++) {
        var e = result[i];
        jsonStr += "  {\n";
        jsonStr += '    "index": ' + e.index + ',\n';
        jsonStr += '    "name": "' + e.name.replace(/"/g, '\\"') + '",\n';
        jsonStr += '    "comments": "' + e.comments.replace(/"/g, '\\"').replace(/\n/g, '\\n') + '",\n';
        jsonStr += '    "timecode": "' + e.timecode + '",\n';
        jsonStr += '    "start_seconds": ' + e.start_seconds.toFixed(3) + ',\n';
        jsonStr += '    "end_seconds": ' + e.end_seconds.toFixed(3) + ',\n';
        jsonStr += '    "duration_seconds": ' + e.duration_seconds.toFixed(3) + ',\n';
        jsonStr += '    "type": "' + e.type + '"\n';
        jsonStr += "  }" + (i < result.length - 1 ? "," : "") + "\n";
    }
    jsonStr += "]\n";

    // 保存先選択
    var saveFile = File.saveDialog("マーカーJSONの保存先", "JSON:*.json");
    if (!saveFile) return;

    saveFile.open("w");
    saveFile.encoding = "UTF-8";
    saveFile.write(jsonStr);
    saveFile.close();

    alert("✅ マーカーエクスポート完了\n" +
          "マーカー数: " + count + "\n" +
          "保存先: " + saveFile.fsName);
})();
