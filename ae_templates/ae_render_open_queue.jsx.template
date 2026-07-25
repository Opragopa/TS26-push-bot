/*
 * Fallback renderer for an already prepared composition in the open project.
 * Rebuilds one Render Queue item because aerender -reuse may leave it stopped.
 */
(function () {
    if (typeof app === "undefined") {
        if (typeof console !== "undefined" && console.log) {
            console.log("ae_render_open_queue.jsx is an After Effects script template and should not be executed directly.");
        }
        return;
    }

    function fail(message) {
        throw new Error("AE open-project render: " + message);
    }

    function readText(file) {
        file.encoding = "UTF-8";
        if (!file.exists || !file.open("r")) fail("Не удалось открыть " + file.fsName);
        var text = file.read();
        file.close();
        return text;
    }

    function writeText(file, text) {
        file.encoding = "UTF-8";
        if (!file.open("w")) fail("Не удалось записать " + file.fsName);
        file.write(String(text));
        file.close();
    }

    function normalizedPath(file) {
        return file.fsName.replace(/\\/g, "/").toLowerCase();
    }

    var paramsPath = typeof __PARAMS_PATH__ === "undefined" ? "" : __PARAMS_PATH__;
    var errorFile = new File(paramsPath + ".render.error");
    var doneFile = new File(paramsPath + ".render.done");

    try {
        if (!paramsPath) fail("Не передан путь к JSON-параметрам.");
        var params = eval("(" + readText(new File(paramsPath)) + ")");
        var source = new File(params.source_project_path);
        if (!app.project || !app.project.file || normalizedPath(app.project.file) !== normalizedPath(source)) {
            fail("В After Effects открыт другой проект.");
        }
        if (app.project.renderQueue.numItems < 1) fail("Render Queue пуст.");

        var comp = app.project.renderQueue.item(1).comp;
        while (app.project.renderQueue.numItems > 0) app.project.renderQueue.item(1).remove();

        var renderItem = app.project.renderQueue.items.add(comp);
        if (params.render_settings_template) renderItem.applyTemplate(params.render_settings_template);
        var outputModule = renderItem.outputModule(1);
        if (params.output_module_template) outputModule.applyTemplate(params.output_module_template);
        outputModule.file = new File(params.output_path);

        app.beginSuppressDialogs();
        app.project.renderQueue.render();
        if (!new File(params.output_path).exists) fail("After Effects завершил очередь без выходного файла.");
        writeText(doneFile, comp.name);
    } catch (error) {
        writeText(errorFile, error && error.toString ? error.toString() : error);
    }
}());
