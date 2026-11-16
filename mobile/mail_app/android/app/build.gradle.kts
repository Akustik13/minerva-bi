import java.util.Properties

plugins {
    id("com.android.application")
    id("kotlin-android")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

// Ключ підпису — поза репозиторієм (той самий, що «Minerva Візитки»): ~/.minerva-android/keystore.properties
// (storeFile, storePassword, keyAlias, keyPassword) або шлях у змінній MINERVA_KEYSTORE_PROPS.
// Без нього release-APK підписується debug-ключем (лише для перевірки, не для людей).
val keyProps = Properties().apply {
    val path = System.getenv("MINERVA_KEYSTORE_PROPS")
        ?: "${System.getProperty("user.home")}/.minerva-android/keystore.properties"
    val f = file(path)
    if (f.exists()) f.inputStream().use { load(it) }
}

android {
    namespace = "com.minervabi.mail"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }

    kotlinOptions {
        jvmTarget = JavaVersion.VERSION_11.toString()
    }

    defaultConfig {
        applicationId = "com.minervabi.mail"
        minSdk = 24
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    signingConfigs {
        if (keyProps.getProperty("storeFile") != null) {
            create("release") {
                storeFile = file(keyProps.getProperty("storeFile"))
                storePassword = keyProps.getProperty("storePassword")
                keyAlias = keyProps.getProperty("keyAlias")
                keyPassword = keyProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
            // телефони — ARM; x86 (емулятор, бібліотеки сканера QR) лише в debug: APK на 11 МБ менший
            ndk { abiFilters += listOf("arm64-v8a", "armeabi-v7a") }
        }
    }
}

flutter {
    source = "../.."
}
