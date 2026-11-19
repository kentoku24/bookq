# kotlinx.serialization — keep generated serializers + companions so
# reflective lookup (KSerializer.serializer()) survives R8 full mode.
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.**
-dontnote kotlinx.serialization.SerializationKt
-keep,includedescriptorclasses class dev.bookq.**$$serializer { *; }
-keepclassmembers class dev.bookq.** {
    *** Companion;
}
-keepclasseswithmembers class dev.bookq.** {
    kotlinx.serialization.KSerializer serializer(...);
}
# OkHttp / Okio bring their own consumer rules; nothing extra needed.
