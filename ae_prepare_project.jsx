/*
 * Receives a JSON job from ae_render_worker.py, changes a temporary copy of
 * the project, and puts exactly one item into its Render Queue.
 */
(function () {
    function fail(message) {
        throw new Error("AE render preparation: " + message);
    }

    function readText(file) {
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

    function findComp(name) {
        for (var index = 1; index <= app.project.numItems; index++) {
            var item = app.project.item(index);
            if (item instanceof CompItem && item.name === name) return item;
        }
        return null;
    }

    function escapeRegExp(text) {
        return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }

    function findSessionComp(pattern, shift) {
        var expression = new RegExp("^" + pattern.replace("{shift}", escapeRegExp(shift)) + "$", "i");
        for (var index = 1; index <= app.project.numItems; index++) {
            var item = app.project.item(index);
            if (item instanceof CompItem && expression.test(item.name)) return item;
        }
        return null;
    }

    function setTextLayers(comp, layers) {
        for (var name in layers) {
            if (!layers.hasOwnProperty(name)) continue;
            var layer = comp.layer(name);
            if (!layer) fail("В композиции '" + comp.name + "' нет слоя '" + name + "'.");
            var property = layer.property("ADBE Text Properties").property("ADBE Text Document");
            if (!property) fail("Слой '" + name + "' не является текстовым.");
            var document = property.value;
            document.text = String(layers[name]);
            property.setValue(document);
        }
    }

    function configureRenderQueue(comp, params) {
        while (app.project.renderQueue.numItems > 0) app.project.renderQueue.item(1).remove();
        var renderItem = app.project.renderQueue.items.add(comp);
        if (params.render_settings_template) renderItem.applyTemplate(params.render_settings_template);
        var outputModule = renderItem.outputModule(1);
        if (params.output_module_template) outputModule.applyTemplate(params.output_module_template);
        outputModule.file = new File(params.output_path);
    }

    function clearRenderQueue() {
        while (app.project.renderQueue.numItems > 0) app.project.renderQueue.item(1).remove();
    }

    function manualPlateLine(name, position) {
        function clean(value) {
            return String(value || "").replace(/[\r\n\t|;]/g, " ").replace(/\s+/g, " ").replace(/^\s+|\s+$/g, "");
        }
        return clean(name) + " | " + clean(position);
    }

    function generatePlaque(params) {
        var generator = new File(params.person_plates_script_path);
        if (!generator.exists) fail("Не найден генератор плашек: " + generator.fsName);

        clearRenderQueue();
        $.global.__sheet2compPersonPlatesPreset = {
            autoRun: true,
            autoConfirm: true,
            silent: true,
            persistSettings: false,
            dataMode: "Вручную",
            manualPeopleText: manualPlateLine(params.plaque_name, params.plaque_position),
            nameField: "ФИО спикера",
            positionField: "Должность",
            photoField: "Фото на плашку",
            shiftField: "",
            shiftFilter: "",
            shiftName: "Запись",
            graphicType: "Плашка",
            compPrefix: "",
            delimiter: "_",
            templateCompName: params.comp_name,
            targetFolderPath: params.plaque_target_folder_path || "",
            nameLayer: params.name_layer,
            nameLayerIndex: "",
            positionLayer: params.position_layer,
            positionLayerIndex: "",
            outputModuleTemplate: params.output_module_template,
            autoImportPhotos: false,
            requirePhotoPrecomp: false,
            recreateExistingComps: false,
            addExistingToRenderQueue: true,
            addToRenderQueue: true
        };
        $.evalFile(generator);

        if (app.project.renderQueue.numItems !== 1) {
            fail("Генератор должен добавить ровно одну плашку в Render Queue, добавлено: " + app.project.renderQueue.numItems + ".");
        }
        var renderItem = app.project.renderQueue.item(1);
        if (params.render_settings_template) renderItem.applyTemplate(params.render_settings_template);
        for (var index = 1; index <= renderItem.numOutputModules; index++) {
            var outputModule = renderItem.outputModule(index);
            if (params.output_module_template) outputModule.applyTemplate(params.output_module_template);
            outputModule.file = new File(params.output_path);
        }
        return renderItem.comp;
    }

    var paramsPath = __PARAMS_PATH__;

    function writeFailure(message) {
        var file = new File(paramsPath + ".error");
        if (file.open("w")) {
            file.write(String(message));
            file.close();
        }
    }

    try {
        var params = eval("(" + readText(new File(paramsPath)) + ")");
        var source = new File(params.source_project_path);
        if (!source.exists) fail("Не найден исходный проект: " + source.fsName);
        app.beginSuppressDialogs();
        var useOpenProject = params.use_open_project === true && app.project && app.project.file && normalizedPath(app.project.file) === normalizedPath(source);
        if (!useOpenProject) app.open(source);
        var comp;
        if (params.kind === "plaque") {
            comp = generatePlaque(params);
        } else {
            comp = findSessionComp(params.session_comp_pattern, params.session_shift);
            if (!comp) fail("Не найдена композиция для задания '" + params.kind + "'.");
            setTextLayers(comp, params.text_layers);
            configureRenderQueue(comp, params);
        }
        if (!comp) fail("Не найдена композиция для задания '" + params.kind + "'.");
        writeText(new File(params.prepared_comp_name_path), comp.name);
        if (useOpenProject) {
            writeText(new File(params.prepared_marker_path), "open");
        } else {
            app.project.save(new File(params.temporary_project_path));
            writeText(new File(params.prepared_marker_path), "temp");
            app.project.close(CloseOptions.DO_NOT_SAVE_CHANGES);
        }
    } catch (error) {
        writeFailure(error && error.toString ? error.toString() : error);
        try {
            if (app.project && !useOpenProject) app.project.close(CloseOptions.DO_NOT_SAVE_CHANGES);
        } catch (closeError) {}
    }
}());
