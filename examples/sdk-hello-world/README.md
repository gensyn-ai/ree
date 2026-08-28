# Gensyn REE SDK Hello World

This example shows how to run a simple "Hello, world!" app using the Gensyn REE SDK.

In this directory are two files:

* `hello_world.py`: Our demo application code that uses the REE SDK.
* `Containerfile`: A tiny container image description that derives from the public REE image and bundles our app.

## Run the app directly

The demo app can be run directly in the REE container like so:

```
docker run --rm \
  -v ~/.cache:/home/gensyn/.cache \
  -v $PWD:/app \
  --entrypoint python3 \
  gensynai/ree:v0.5.0 \
  /app/hello_world.py
```

This command binds the cache and code directories, sets the container's initial command to `python3`, and runs `/app/hello_world.py` as an argument. This style is recommended for fast iteration on apps that don't need any additional dependencies in the container.

## Use a custom app image

Apps that need additional dependencies or need more control over the container environment can build a custom image derived from the public REE image. In this example, we're just changing the initial command the container runs to make the default invocation a little cleaner.

Build the app into a custom image:

```
docker build -f Containerfile -t ree-sdk-hello-world .
```

Run the app that's now bundled in the custom image:

```
docker run --rm -v ~/.cache:/home/gensyn/.cache ree-sdk-hello-world
```

## Putting it all together

These two approaches aren't mutually exclusive! We can define our own derived image, but can also run arbitrary code in it without rebuilding the image every time. In that case, you only rebuild the derived image when its Containerfile changes. Then, use the direct syntax to run your app.
