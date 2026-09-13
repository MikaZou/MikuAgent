# 本项目没有反射/序列化需求，保持默认即可。
# 模型与页面都在 assets 里，R8 不会碰它们。

# WebView 的 JS 桥：方法名不能被混淆，否则 @JavascriptInterface 会失效。
-keepclassmembers class com.mikuagent.pet.web.Bridge {
    public *;
}
-keepattributes JavascriptInterface
