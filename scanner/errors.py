from django.shortcuts import render


def forbidden(request, message="You don't have permission to do that."):
    return render(request, "403.html", {"message": message}, status=403)
