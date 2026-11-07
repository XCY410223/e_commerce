"""订单评论表单。"""

from django import forms

from .models import OrderComment


class OrderCommentForm(forms.Form):
    """校验用户提交的评论内容，服务层会再次校验。"""

    content = forms.CharField(
        label="评论内容",
        max_length=OrderComment.MAX_CONTENT_LENGTH,
        strip=True,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "maxlength": OrderComment.MAX_CONTENT_LENGTH,
                "placeholder": "说说这次购物的感受吧",
            }
        ),
        error_messages={
            "required": "请输入评论内容",
            "max_length": f"评论内容不能超过 {OrderComment.MAX_CONTENT_LENGTH} 个字符",
        },
    )

    def clean_content(self):
        content = self.cleaned_data["content"].strip()
        if not content:
            raise forms.ValidationError("评论内容不能为空")
        return content
