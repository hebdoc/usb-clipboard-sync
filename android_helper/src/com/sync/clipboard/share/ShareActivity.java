package com.sync.clipboard.share;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.widget.Toast;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;

public class ShareActivity extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        try {
            Intent intent = getIntent();
            String action = intent != null ? intent.getAction() : null;
            if (Intent.ACTION_SEND.equals(action)) {
                Uri uri = (Uri) intent.getParcelableExtra(Intent.EXTRA_STREAM);
                if (uri != null) {
                    // 1. 写入系统剪贴板
                    ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                    ClipData clip = ClipData.newUri(getContentResolver(), "image", uri);
                    cm.setPrimaryClip(clip);

                    // 2. 写入截屏临时目录供 PC 实时通道拉取
                    try {
                        File dir = new File("/sdcard/DCIM/Screenshots");
                        if (!dir.exists()) dir.mkdirs();
                        File dest = new File(dir, "Screenshot_ToPC_" + System.currentTimeMillis() + ".jpg");
                        try (InputStream in = getContentResolver().openInputStream(uri);
                             FileOutputStream out = new FileOutputStream(dest)) {
                            byte[] buf = new byte[16384];
                            int n;
                            while ((n = in.read(buf)) != -1) {
                                out.write(buf, 0, n);
                            }
                        }
                    } catch (Exception ignored) {
                    }

                    Toast.makeText(this, "已发送至电脑剪贴板！", Toast.LENGTH_SHORT).show();
                }
                finish();
            } else {
                // 桌面点击直接打开时，弹出友好使用指南
                new AlertDialog.Builder(this)
                    .setTitle("发送到电脑 - 快捷组件")
                    .setMessage("组件已就绪！\n\n【相册原图同步】\n在手机【相册】或【文件管理】中打开照片，点击左下角【发送】(分享)，选择【发送到电脑】，电脑端即可直接 Ctrl + V 粘贴！\n\n【三指截屏秒同步】\n在手机任意界面三指下滑截屏，电脑也会自动同步到剪贴板。")
                    .setPositiveButton("知道了", new DialogInterface.OnClickListener() {
                        @Override
                        public void onClick(DialogInterface dialog, int which) {
                            finish();
                        }
                    })
                    .setOnCancelListener(new DialogInterface.OnCancelListener() {
                        @Override
                        public void onCancel(DialogInterface dialog) {
                            finish();
                        }
                    })
                    .show();
            }
        } catch (Exception e) {
            Toast.makeText(this, "提示: " + e.getMessage(), Toast.LENGTH_SHORT).show();
            finish();
        }
    }
}
