import groovy.json.JsonSlurper
import java.security.MessageDigest

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// b515 CANDIDATE-ONLY (DO NOT MERGE): pin for the bundled-PWA `candidate` build type. The committed expected manifest
// names the accepted MarketVivi commit and the sha256 of each runtime file. Debug/release do not read the bundle.
val candidatePwaManifestFile = file("src/candidate/pwa_expected_manifest.json")
val candidatePwaRuntimeFiles = listOf("index.html", "app.js", "api.js", "log-viewer.js", "style.css", "manifest.json")
fun candidateSha256(bytes: ByteArray): String =
    MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
@Suppress("UNCHECKED_CAST")
val candidatePwaManifest: Map<String, Any?> =
    JsonSlurper().parse(candidatePwaManifestFile) as Map<String, Any?>
val candidatePwaSourceHead = candidatePwaManifest["marketvivi_sha"] as String
val candidatePwaManifestSha256 = candidateSha256(candidatePwaManifestFile.readBytes())
val candidatePwaStagedDir = layout.buildDirectory.dir("generated/candidatePwa/assets")

android {
    namespace = "com.marketradar.app"
    compileSdk = 35
    
    buildFeatures {
        buildConfig = true
    }

    defaultConfig {
        // Keep release retries rooted in this file because GitHub signed-release
        // workflow is path-filtered to app/build.gradle.kts.
        // b503 / 2.6.72: capture reliability (frozen requests, durable journal, late delivery, capture events).
        // b502 / 2.6.71: snapshot-stage diagnostics, generated-row recovery and dated secondary valuation.
        // b501 / 2.6.70: retry frozen brain snapshots after transient upload failures.
        // b500 / 2.6.69: Evidence Batch 1 paper decision-menu evidence (durable outbox, keyset readback, recovery).
        // b499 / 2.6.68: bounded evaluation evidence and explicit model-learning status.
        // b473 / 2.6.42: Paper integrity, contract identity, export, and review gates.
        // b472 / 2.6.41: G6 versioned performance ledger + shadow comparisons.
        // b479 / 2.6.48: ML eval lease recovery (stable holder + releaseLease + historical continuation session_date)
        // b480 / 2.6.49: Eng batches A–E (lot authority, C3 honesty, eval completeness, teacher reporting, holding horizon)
        // b481 / 2.6.50: Codex R1 fix — identity completeness gates, Supabase identity readback, PWA labelsSaved truth, teacher_summary, lot failure reasons
        // b493 / 2.6.62: persist fetched chain evidence before Brain; fail closed on incomplete H2 marks
        // b494 / 2.6.63: verify all-WAIT research outcomes without inventing training labels
        // b495 / 2.6.64: add registered-device compact PC2 dual-write alongside legacy evidence
        // b497 / 2.6.66: retire afternoon positioning;  correct Paper VIX history authority and fail-closed decisions
        // b492 / 2.6.61: Brain completion/freshness safety + numeric snapshot null sanitization
        // b490 / 2.6.59: B1 capture evidence + B3/B3.1 position mark trust (wide book, mid-fallback stop), decisions 1/3/5/9/10
        // b489 / 2.6.58: C3 OOM-safe percentile finalization (keyset paging, fail-closed local fallback, trim marker)
        // b488 / 2.6.57: Position tick flush R7–R9 (fail-closed 409, full fingerprint, overflow UI + overflow_active fix)
        // b485 / 2.6.54: synchronized release for the fresh P1 Paper-close quote contract
        // b484 / 2.6.53: expose verified Paper marks through the APK bridge and refresh on P1 ticks
        // b483 / 2.6.52: preserve strict verified Paper marks across temporary Brain-chain gaps
        // b482 / 2.6.51: exact frozen-primary completion, fail-closed resolver-only lots, uncertain teacher verdict suppression
        // b471 / 2.6.40: G5 durable evening-stage ledger + truthful completion.
        // b470 / 2.6.39: G4 net-target + executable exit-policy contract.
        // b460: Notification audit fixes — Book Profit force_alignment, entry 09:15,
        // position-tick shadow alerts, eval append guard, rejected-id dedupe.
        applicationId = "com.marketradar.app"
        minSdk = 26
        targetSdk = 35
        versionCode = 517
        versionName = "2.6.86"
        
        ndk {
            abiFilters += listOf("armeabi-v7a", "arm64-v8a", "x86", "x86_64")
        }
        
        buildConfigField("String", "SUPABASE_ANON_KEY", "\"${System.getenv("SUPABASE_ANON_KEY") ?: project.findProperty("SUPABASE_ANON_KEY") ?: ""}\"")
        // Stage 2 CI-2: full source head recorded in the research export manifest (git HEAD / GITHUB_SHA at build time).
        val mrSourceHead = System.getenv("MR_SOURCE_HEAD") ?: System.getenv("GITHUB_SHA") ?: try {
            ProcessBuilder("git", "rev-parse", "HEAD").directory(rootDir).start().inputStream.bufferedReader().readText().trim()
        } catch (e: Exception) { "unknown" }
        buildConfigField("String", "SOURCE_HEAD", "\"${mrSourceHead.ifBlank { "unknown" }}\"")
        buildConfigField("String", "SUPABASE_URL", "\"https://fdynxkfxohbnlvayouje.supabase.co\"")
        // b515 CANDIDATE-ONLY: off for debug/release; only the `candidate` build type below turns bundled routing on.
        buildConfigField("boolean", "PWA_CANDIDATE", "false")
        buildConfigField("String", "PWA_SOURCE_HEAD", "\"none\"")
        buildConfigField("String", "PWA_MANIFEST_SHA256", "\"none\"")
    }

    signingConfigs {
        create("release") {
            storeFile = file(System.getenv("RELEASE_KEYSTORE_PATH") ?: "keystore.jks")
            storePassword = System.getenv("RELEASE_KEYSTORE_PASSWORD")
            keyAlias = System.getenv("RELEASE_KEY_ALIAS")
            keyPassword = System.getenv("RELEASE_KEY_PASSWORD")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
        // b515 CANDIDATE-ONLY (DO NOT MERGE): same applicationId (no suffix), same release signing config, not debuggable,
        // so a signed candidate installs IN PLACE over the existing app and keeps its data. Bundles the pinned PWA.
        create("candidate") {
            initWith(getByName("release"))
            signingConfig = signingConfigs.getByName("release")
            matchingFallbacks += listOf("release")
            buildConfigField("boolean", "PWA_CANDIDATE", "true")
            buildConfigField("String", "PWA_SOURCE_HEAD", "\"$candidatePwaSourceHead\"")
            buildConfigField("String", "PWA_MANIFEST_SHA256", "\"$candidatePwaManifestSha256\"")
        }
    }

    // b515 CANDIDATE-ONLY: bundle bytes are staged at build time (tools/candidate_pwa/stage_candidate_pwa.py) into
    // build/generated, never committed; verifyCandidatePwaBundle re-checks them before the candidate assets merge.
    sourceSets.getByName("candidate").assets.srcDir(candidatePwaStagedDir)

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    packaging {
        jniLibs {
            useLegacyPackaging = false
        }
    }
}

// b515 CANDIDATE-ONLY: build-time verification of the staged bundle against the committed pin. Wired only into
// candidate asset merging, so debug/release (and their unit tests) never depend on it.
val verifyCandidatePwaBundle by tasks.registering {
    group = "verification"
    description = "Fail unless build/generated/candidatePwa holds exactly the pinned PWA runtime bytes."
    inputs.file(candidatePwaManifestFile)
    inputs.dir(candidatePwaStagedDir).optional()
    doLast {
        val dir = candidatePwaStagedDir.get().asFile.resolve("pwa_candidate")
        if (!dir.isDirectory) throw GradleException("CANDIDATE_PWA_ERROR: bundle not staged at $dir (run tools/candidate_pwa/stage_candidate_pwa.py)")
        val present = (dir.list() ?: emptyArray()).sorted()
        val wanted = (candidatePwaRuntimeFiles + "candidate_manifest.json").sorted()
        if (present != wanted) throw GradleException("CANDIDATE_PWA_ERROR: bundle dir must contain exactly $wanted, found $present")
        if (!dir.resolve("candidate_manifest.json").readBytes().contentEquals(candidatePwaManifestFile.readBytes()))
            throw GradleException("CANDIDATE_PWA_ERROR: packaged candidate_manifest.json differs from the committed pin")
        @Suppress("UNCHECKED_CAST")
        val files = candidatePwaManifest["files"] as Map<String, Map<String, Any?>>
        if (files.keys.sorted() != candidatePwaRuntimeFiles.sorted()) throw GradleException("CANDIDATE_PWA_ERROR: pin is not the runtime allowlist")
        for (name in candidatePwaRuntimeFiles) {
            val bytes = dir.resolve(name).readBytes()
            val got = candidateSha256(bytes)
            val want = files.getValue(name)["sha256"] as String
            val wantBytes = (files.getValue(name)["bytes"] as Number).toInt()
            if (got != want || bytes.size != wantBytes) throw GradleException("CANDIDATE_PWA_ERROR: $name sha256=$got bytes=${bytes.size} != pinned $want/$wantBytes")
            println("CANDIDATE_PWA_VERIFIED $got  $name")
        }
        println("CANDIDATE_PWA_VERIFIED marketvivi=$candidatePwaSourceHead manifest_sha256=$candidatePwaManifestSha256")
    }
}
tasks.matching { it.name == "mergeCandidateAssets" || it.name == "preCandidateBuild" }.configureEach {
    dependsOn(verifyCandidatePwaBundle)
}

chaquopy {
    defaultConfig {
        version = "3.11"
        buildPython("python") // ensures it finds default python from local system if needed
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("androidx.activity:activity-ktx:1.8.0")
    implementation("androidx.webkit:webkit:1.10.0")
    implementation("androidx.swiperefreshlayout:swiperefreshlayout:1.1.0")
    
    // Networking & Background Processing
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.squareup.okhttp3:logging-interceptor:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.7.3")
    // B1.1 R2: durable pending-tick drain (unique one-time work, CONNECTED constraint).
    implementation("androidx.work:work-runtime:2.9.1")
    
    // UI & Material Design
    implementation("com.google.android.material:material:1.11.0")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
