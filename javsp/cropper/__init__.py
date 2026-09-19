from javsp.cropper.interface import Cropper, DefaultCropper

def get_cropper(engine = None) -> Cropper:
    return DefaultCropper()
