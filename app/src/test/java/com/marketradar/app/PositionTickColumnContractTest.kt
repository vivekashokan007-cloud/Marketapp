package com.marketradar.app

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class PositionTickColumnContractTest {
    @Test fun positionTickTopLevelProducerKeysExistInExportedSchemaContract() {
        // buildTickRow directly constructs its output via literal put/putOptNumber
        // calls. Read that return block so future additions fail this contract test.
        val producerFile = listOf(
            File("src/main/java/com/marketradar/app/PositionTickService.kt"),
            File("app/src/main/java/com/marketradar/app/PositionTickService.kt")
        ).first { it.isFile }
        val source = producerFile.readText()
        val method = source.substringAfter("private fun buildTickRow(")
            .substringBefore("\n    /**\n     * Percentile-contextual exit levels")
        val returnedObject = method.substringAfter("return JSONObject().apply {")
        val rowKeys = Regex("(?:put|putOptNumber)\\(\"([^\"]+)\"")
            .findAll(returnedObject).map { it.groupValues[1] }.toSet()
        assertTrue("buildTickRow return block was not found", rowKeys.size > 20)
        val schemaFile = listOf(
            File("../docs/contracts/position_ticks_columns.json"),
            File("docs/contracts/position_ticks_columns.json")
        ).first { it.isFile }
        val schemaText = schemaFile.readText()
        val schema = JSONObject(schemaText).getJSONArray("columns")
        val columnNames = (0 until schema.length()).map { schema.getString(it) }.toSet()
        assertEquals("Producer keys absent from position_ticks schema: ${rowKeys - columnNames}",
            emptySet<String>(), rowKeys - columnNames)
    }
}
